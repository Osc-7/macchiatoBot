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
    chat_id_hint: Optional[str] = None,
) -> Optional[str]:
    """群聊 session_id 自带 chat_id；私聊依赖 CoreEntry.feishu_chat_id（含 zombie）或显式 hint。"""
    hint = str(chat_id_hint or "").strip()
    if hint:
        return hint
    sid = (session_id or "").strip()
    if sid.startswith("feishu:chat:"):
        rest = sid.split(":", 2)
        if len(rest) >= 3:
            return rest[2].strip() or None
    if core_pool is not None:
        ent = None
        get_entry = getattr(core_pool, "get_entry", None)
        if callable(get_entry):
            ent = get_entry(sid)
        else:
            get_live = getattr(core_pool, "get_live_entry", None)
            if callable(get_live):
                ent = get_live(sid)
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
            on_outgoing_attachment=self._on_outgoing_attachment,
        )

    @property
    def hooks(self) -> AgentHooks:
        return self._hooks

    def _ck_next_seq(self) -> int:
        self._ck_seq += 1
        return self._ck_seq

    def _visible_assistant_text(self) -> str:
        """流式框只应在有可见正文时出现；纯空白/换行不算。"""
        return (self._assistant_buffer or "").strip()

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
        if not self._visible_assistant_text():
            return
        body = self._assistant_buffer
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
        if not self._visible_assistant_text():
            return
        text = self._assistant_buffer
        if len(text) > 100_000:
            text = text[:100_000]
        await self._feishu_client.cardkit_put_streaming_text_content(
            card_id=self._ck_card_id,
            element_id=AGENT_REPLY_STREAM_ELEMENT_ID,
            content=text,
            sequence=self._ck_next_seq(),
        )
        self._assistant_stream_patched_len = len(self._assistant_buffer)

    async def _ck_stop_streaming_mode(self) -> None:
        """官方关流：PATCH settings 把 streaming_mode 设为 false。

        只 replace 卡片 JSON 不会清掉客户端会话标题上的 (Streaming) / [生成中...]。
        """
        if not self._ck_card_id or not self._feishu_client:
            return
        await self._feishu_client.cardkit_patch_card_settings(
            card_id=self._ck_card_id,
            settings={"config": {"streaming_mode": False}},
            sequence=self._ck_next_seq(),
        )

    def _ck_release_local_handle(self) -> None:
        self._ck_card_id = ""
        self._ck_seq = 0
        self._assistant_stream_patched_len = 0

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
        # 先关流式模式，再全量替换 header 标签（Streaming → Complete/Segment）。
        await self._ck_stop_streaming_mode()
        try:
            await self._feishu_client.cardkit_replace_card_entity(
                card_id=self._ck_card_id,
                card=card,
                sequence=self._ck_next_seq(),
            )
        except Exception as exc:  # noqa: BLE001
            mid = self._assistant_stream_mid
            if not mid:
                raise
            logger.warning(
                "feishu CardKit replace after stream stop failed, "
                "fallback to message PATCH: %s",
                exc,
            )
            await self._feishu_client.patch_interactive_card_message(
                message_id=mid, card=card
            )
        self._ck_release_local_handle()

    async def _ck_bootstrap(self) -> None:
        fc = self._feishu_client
        if not fc or not self._chat_id:
            return
        try:
            shell = build_agent_reply_card_streaming_shell(reply_phase="streaming")
            cid = await fc.create_cardkit_card_entity(card=shell)
            mid = await fc.send_message_with_card_id(chat_id=self._chat_id, card_id=cid)
            self._ck_card_id = cid
            self._ck_seq = 0
            if mid:
                self._assistant_stream_mid = mid
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
        if not self._visible_assistant_text():
            # 思考模型常先打出空白/换行 delta，会留下空的 Streaming 壳。
            # 进工具前丢掉这张空卡的本地句柄，避免后续终态 PATCH 回填到工具卡片上方。
            # 句柄丢掉之前仍要关 streaming_mode，否则会话标题会一直停在 (Streaming)。
            if self._use_cardkit_stream and not self._ck_fallback and self._ck_card_id:
                try:
                    await self._ck_stop_streaming_mode()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "feishu CardKit stop streaming on empty shell failed: %s",
                        exc,
                    )
            self._assistant_buffer = ""
            self._assistant_stream_mid = ""
            self._ck_release_local_handle()
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
        if not self._visible_assistant_text():
            return
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
                try:
                    await self._ck_put_buffer()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("feishu CardKit stream put failed: %s", exc)
                return

            async def _debounced_ck() -> None:
                try:
                    await asyncio.sleep(_FEISHU_STREAM_PATCH_DEBOUNCE_S)
                    await self._ck_put_buffer()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    logger.warning("feishu CardKit stream put failed: %s", exc)

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

    async def _on_outgoing_attachment(self, att: Dict[str, Any]) -> bool:
        """工具调用后立即把附件发到飞书；成功后才算送达。"""
        from agent_core.agent.media_helpers import is_attachment_delivered, mark_attachment_delivered

        if not self._feishu_client or not self._chat_id:
            return False
        if is_attachment_delivered(att):
            return True
        ok, err = await self._feishu_client.send_reply_attachment(
            chat_id=self._chat_id,
            att=att,
        )
        if not ok:
            logger.warning(
                "feishu immediate attachment send failed: %s", err or "unknown"
            )
            return False
        mark_attachment_delivered(att)
        return True

    async def _on_trace_event(self, evt: Dict[str, Any]) -> None:
        if not self._feishu_client or not self._chat_id:
            return
        evt_type = str(evt.get("type") or "")
        fc = self._feishu_client
        chat_id = self._chat_id

        if evt_type == "tool_call" and self._tool_trace_cards_enabled:
            await self._end_assistant_segment_before_tools()
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
            await self._end_assistant_segment_before_tools()
            return

        if evt_type == "chat_history_summarized":
            note = str(evt.get("message") or "").strip() or "Chat History Summarized."
            try:
                await fc.send_text_message(chat_id=chat_id, text=note)
            except Exception as exc:  # noqa: BLE001
                logger.warning("feishu chat_history_summarized notify failed: %s", exc)
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
