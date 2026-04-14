"""
飞书单轮对话：流式助手卡片 + 工具 trace（AgentHooks + 结束后 finalize）。

供 FeishuIPCBridge.send_message 与 CorePool inject_turn（父会话）复用。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional

from agent_core.config import get_config
from agent_core.interfaces import AgentHooks, AgentRunResult

from .client import FeishuClient
from .interactive_cards import (
    AGENT_REPLY_STREAM_ELEMENT_ID,
    AssistantReplyPhase,
    build_agent_reply_card_streaming_shell,
    build_agent_reply_markdown_card,
    build_tool_call_pending_card,
    build_tool_trace_card,
)

if TYPE_CHECKING:
    from system.kernel.core_pool import CorePool

logger = logging.getLogger(__name__)

_FEISHU_STREAM_PATCH_DEBOUNCE_S = 0.12
_FEISHU_STREAM_PATCH_MIN_CHARS = 48


def resolve_feishu_chat_id_for_session(
    session_id: str,
    *,
    core_pool: Optional["CorePool"] = None,
) -> Optional[str]:
    """群聊 session_id 自带 chat_id；私聊依赖 CoreEntry.feishu_chat_id（由首轮用户消息写入）。"""
    sid = (session_id or "").strip()
    if sid.startswith("feishu:chat:"):
        rest = sid.split(":", 2)
        if len(rest) >= 3:
            return rest[2].strip() or None
    if core_pool is not None:
        ent = core_pool.get_live_entry(sid)
        if ent is not None:
            fc = getattr(ent, "feishu_chat_id", None)
            if fc:
                return str(fc).strip() or None
    return None


class FeishuTurnHooksController:
    """
    构建 on_assistant_delta / on_trace_event，并在 kernel.run 结束后关闭流式壳或 PATCH 终态。
    """

    def __init__(
        self,
        *,
        chat_id: str,
        feishu_client: Optional[FeishuClient] = None,
        timeout_seconds: Optional[float] = None,
        markdown_header_title: str = "回复",
    ) -> None:
        cid = (chat_id or "").strip()
        if not cid:
            raise ValueError("chat_id 不能为空")
        fei = get_config().feishu
        if feishu_client is None:
            http_to = (
                float(timeout_seconds)
                if timeout_seconds is not None
                else max(float(fei.timeout_seconds), 120.0)
            )
            feishu_client = FeishuClient(timeout_seconds=http_to)
        self._feishu_client = feishu_client
        self._chat_id = cid
        self._markdown_header_title = markdown_header_title

        self._tool_trace_cards_enabled = bool(fei.tool_trace_cards_enabled)
        _rf = (fei.reply_format or "markdown_card").strip().lower()
        self._stream_reply = bool(
            _rf == "markdown_card" and bool(getattr(fei, "assistant_reply_stream", True))
        )
        self._use_cardkit_stream = bool(
            self._stream_reply and bool(getattr(fei, "assistant_cardkit_stream", True))
        )

        self._pending_tool_args: Dict[str, Dict[str, Any]] = {}
        self._pending_tool_message_ids: Dict[str, str] = {}

        self._assistant_buffer: str = ""
        self._assistant_stream_mid: str = ""
        self._assistant_stream_patched_len: int = 0
        self._assistant_debounce_task: Optional[asyncio.Task] = None
        self._ck_card_id: str = ""
        self._ck_seq: int = 0
        self._ck_fallback: bool = False

        self._hooks = AgentHooks(
            on_assistant_delta=self._on_assistant_delta,
            on_trace_event=self._on_trace_event,
            on_feishu_ask_user_notify=self._on_feishu_ask_user_notify,
            on_feishu_permission_notify=self._on_feishu_permission_notify,
        )

    @property
    def hooks(self) -> AgentHooks:
        return self._hooks

    def _ck_next_seq(self) -> int:
        self._ck_seq += 1
        return self._ck_seq

    async def _cancel_assistant_debounce(self) -> None:
        t = self._assistant_debounce_task
        self._assistant_debounce_task = None
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def _apply_assistant_reply_stream_card(
        self,
        *,
        reply_phase: AssistantReplyPhase = "streaming",
    ) -> None:
        fc = self._feishu_client
        if not fc or not self._chat_id:
            return
        body = self._assistant_buffer
        if not body:
            return
        card = build_agent_reply_markdown_card(
            body,
            header_title=self._markdown_header_title,
            reply_phase=reply_phase,
        )
        try:
            if self._assistant_stream_mid:
                await fc.patch_interactive_card_message(
                    message_id=self._assistant_stream_mid, card=card
                )
            else:
                mid = await fc.send_interactive_card(chat_id=self._chat_id, card=card)
                if mid:
                    self._assistant_stream_mid = mid
        except Exception as exc:  # noqa: BLE001
            logger.warning("feishu assistant stream card failed: %s", exc)

    async def _apply_stream_patch_and_mark(self) -> None:
        await self._apply_assistant_reply_stream_card(reply_phase="streaming")
        self._assistant_stream_patched_len = len(self._assistant_buffer)

    async def _ck_put_buffer(self) -> None:
        if not self._ck_card_id or not self._feishu_client:
            return
        text = self._assistant_buffer
        if not text:
            return
        if len(text) > 100_000:
            text = text[:100_000]
        await self._feishu_client.cardkit_put_streaming_text_content(
            card_id=self._ck_card_id,
            element_id=AGENT_REPLY_STREAM_ELEMENT_ID,
            content=text,
            sequence=self._ck_next_seq(),
        )
        self._assistant_stream_patched_len = len(self._assistant_buffer)

    async def _ck_close_streaming_card(
        self,
        last_plain: str,
        *,
        close_kind: Literal["segment", "final"],
    ) -> None:
        if not self._ck_card_id or not self._feishu_client:
            return
        text = last_plain or ""
        if len(text) > 100_000:
            text = text[:100_000]
        phase: AssistantReplyPhase = (
            "final" if close_kind == "final" else "segment"
        )
        card = build_agent_reply_markdown_card(
            text,
            header_title=self._markdown_header_title,
            reply_phase=phase,
        )
        cfg = card.setdefault("config", {})
        cfg["streaming_mode"] = False
        cfg.pop("streaming_config", None)
        await self._feishu_client.cardkit_replace_card_entity(
            card_id=self._ck_card_id,
            card=card,
            sequence=self._ck_next_seq(),
        )
        self._ck_card_id = ""
        self._ck_seq = 0
        self._assistant_stream_patched_len = 0

    async def _ck_bootstrap(self) -> None:
        fc = self._feishu_client
        if not fc or not self._chat_id:
            return
        try:
            shell = build_agent_reply_card_streaming_shell(reply_phase="streaming")
            cid = await fc.create_cardkit_card_entity(card=shell)
            await fc.send_message_with_card_id(chat_id=self._chat_id, card_id=cid)
            self._ck_card_id = cid
            self._ck_seq = 0
            await self._ck_put_buffer()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "feishu CardKit stream unavailable, fallback to message PATCH: %s",
                exc,
            )
            self._ck_fallback = True
            self._ck_card_id = ""
            self._ck_seq = 0
            await self._apply_stream_patch_and_mark()

    async def _flush_assistant_buffer_legacy(self) -> None:
        if not self._feishu_client or not self._chat_id:
            self._assistant_buffer = ""
            return
        from .reply_dispatch import send_feishu_agent_reply

        text_out = self._assistant_buffer.strip()
        if not text_out:
            self._assistant_buffer = ""
            return
        try:
            await send_feishu_agent_reply(
                client=self._feishu_client,
                chat_id=self._chat_id,
                output_text=text_out,
                markdown_card_header_title=self._markdown_header_title,
                reply_phase="segment",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "failed to send feishu intermediate assistant message: %s", exc
            )
        finally:
            self._assistant_buffer = ""

    async def _end_assistant_segment_before_tools(self) -> None:
        await self._cancel_assistant_debounce()
        if not self._assistant_buffer.strip():
            self._assistant_buffer = ""
            self._assistant_stream_mid = ""
            self._assistant_stream_patched_len = 0
            self._ck_card_id = ""
            self._ck_seq = 0
            return
        if self._stream_reply:
            if self._use_cardkit_stream and not self._ck_fallback and self._ck_card_id:
                await self._ck_close_streaming_card(
                    self._assistant_buffer, close_kind="segment"
                )
                self._assistant_buffer = ""
                self._assistant_stream_mid = ""
                return
            if self._assistant_stream_mid:
                await self._apply_assistant_reply_stream_card(reply_phase="segment")
            else:
                await self._flush_assistant_buffer_legacy()
            self._assistant_buffer = ""
            self._assistant_stream_mid = ""
            self._assistant_stream_patched_len = 0
        else:
            await self._flush_assistant_buffer_legacy()

    async def _on_assistant_delta(self, delta: str) -> None:
        if not delta:
            return
        self._assistant_buffer += delta
        if (
            not self._stream_reply
            or not self._feishu_client
            or not self._chat_id
        ):
            return

        if self._use_cardkit_stream and not self._ck_fallback:
            if not self._ck_card_id:
                await self._ck_bootstrap()
                return
            pending_chars = len(self._assistant_buffer) - self._assistant_stream_patched_len
            if pending_chars >= _FEISHU_STREAM_PATCH_MIN_CHARS:
                await self._cancel_assistant_debounce()
                await self._ck_put_buffer()
                return

            async def _debounced_ck() -> None:
                try:
                    await asyncio.sleep(_FEISHU_STREAM_PATCH_DEBOUNCE_S)
                    await self._ck_put_buffer()
                except asyncio.CancelledError:
                    pass

            await self._cancel_assistant_debounce()
            self._assistant_debounce_task = asyncio.create_task(_debounced_ck())
            return

        if not self._assistant_stream_mid:
            await self._apply_stream_patch_and_mark()
            return

        pending_chars = len(self._assistant_buffer) - self._assistant_stream_patched_len
        if pending_chars >= _FEISHU_STREAM_PATCH_MIN_CHARS:
            await self._cancel_assistant_debounce()
            await self._apply_stream_patch_and_mark()
            return

        async def _debounced() -> None:
            try:
                await asyncio.sleep(_FEISHU_STREAM_PATCH_DEBOUNCE_S)
                await self._apply_stream_patch_and_mark()
            except asyncio.CancelledError:
                pass

        await self._cancel_assistant_debounce()
        self._assistant_debounce_task = asyncio.create_task(_debounced())

    async def _on_feishu_ask_user_notify(
        self, batch_id: str, payload: Dict[str, Any]
    ) -> None:
        """由 IPC 流在 tool trace 之后顺序调用，与 daemon 直发飞书解耦。"""
        from .ask_user_notify import send_ask_user_feishu_cards

        await send_ask_user_feishu_cards(batch_id, payload)

    async def _on_feishu_permission_notify(
        self, permission_id: str, payload: Dict[str, Any]
    ) -> None:
        from .permission_notify import send_permission_feishu_card

        await send_permission_feishu_card(permission_id, payload)

    async def _on_trace_event(self, evt: Dict[str, Any]) -> None:
        if not self._feishu_client or not self._chat_id:
            return
        evt_type = str(evt.get("type") or "")
        fc = self._feishu_client
        chat_id = self._chat_id

        if evt_type == "tool_call" and self._tool_trace_cards_enabled:
            tcid = str(evt.get("tool_call_id") or "").strip()
            if tcid:
                self._pending_tool_args[tcid] = {
                    "name": str(evt.get("name") or ""),
                    "arguments": evt.get("arguments"),
                }
                try:
                    name_call = str(evt.get("name") or "").strip() or "unknown"
                    card_pending = build_tool_call_pending_card(
                        tool_name=name_call,
                        arguments=evt.get("arguments"),
                        tool_call_id=tcid,
                    )
                    mid = await fc.send_interactive_card(
                        chat_id=chat_id, card=card_pending
                    )
                    if mid:
                        self._pending_tool_message_ids[tcid] = mid
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "failed to send feishu tool_call pending card (tool=%s): %s",
                        evt.get("name"),
                        exc,
                    )
            return

        if evt_type == "tool_result" and self._tool_trace_cards_enabled:
            tcid = str(evt.get("tool_call_id") or "").strip()
            meta = self._pending_tool_args.pop(tcid, None) if tcid else None
            msg_id = (
                self._pending_tool_message_ids.pop(tcid, "") if tcid else ""
            ).strip()
            name = str(
                evt.get("name") or (meta or {}).get("name") or ""
            ).strip() or "unknown"
            try:
                dp_raw = evt.get("data_preview")
                data_preview = (
                    str(dp_raw).strip()
                    if isinstance(dp_raw, str) and dp_raw.strip()
                    else None
                )
                args_resolved = (meta or {}).get("arguments")
                if args_resolved is None:
                    args_resolved = evt.get("arguments")
                card = build_tool_trace_card(
                    tool_name=name,
                    success=bool(evt.get("success")),
                    message=str(evt.get("message") or ""),
                    duration_ms=int(evt.get("duration_ms") or 0),
                    error=evt.get("error"),
                    data_preview=data_preview,
                    arguments=args_resolved,
                    tool_call_id=tcid or None,
                )
                if msg_id:
                    try:
                        await fc.patch_interactive_card_message(
                            message_id=msg_id, card=card
                        )
                    except Exception as patch_exc:  # noqa: BLE001
                        logger.warning(
                            "feishu patch tool card failed (tool=%s), send new: %s",
                            name,
                            patch_exc,
                        )
                        await fc.send_interactive_card(chat_id=chat_id, card=card)
                else:
                    await fc.send_interactive_card(chat_id=chat_id, card=card)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "failed to send feishu tool trace card (tool=%s): %s",
                    name,
                    exc,
                )
            return

        if evt_type == "llm_request":
            if self._assistant_buffer.strip():
                await self._end_assistant_segment_before_tools()
            return

    async def finalize_after_run(self, result: AgentRunResult) -> AgentRunResult:
        """关闭流式壳或 PATCH 终态，并设置 feishu_skip_final_reply 供 poll_push 跳过重复发送。"""
        await self._cancel_assistant_debounce()
        out_text = (result.output_text or "").strip()
        fc = self._feishu_client
        cid = self._chat_id

        if (
            self._use_cardkit_stream
            and not self._ck_fallback
            and fc
            and cid
            and self._ck_card_id
            and out_text
        ):
            try:
                await self._ck_close_streaming_card(out_text, close_kind="final")
                meta = dict(result.metadata)
                meta["feishu_skip_final_reply"] = True
                return replace(result, metadata=meta)
            except Exception as exc:  # noqa: BLE001
                logger.warning("feishu CardKit final close failed: %s", exc)

        if (
            self._stream_reply
            and fc
            and cid
            and self._assistant_stream_mid
            and out_text
        ):
            try:
                card = build_agent_reply_markdown_card(
                    out_text,
                    header_title=self._markdown_header_title,
                    reply_phase="final",
                )
                await fc.patch_interactive_card_message(
                    message_id=self._assistant_stream_mid, card=card
                )
                meta = dict(result.metadata)
                meta["feishu_skip_final_reply"] = True
                return replace(result, metadata=meta)
            except Exception as exc:  # noqa: BLE001
                logger.warning("feishu final assistant stream patch failed: %s", exc)

        return result
