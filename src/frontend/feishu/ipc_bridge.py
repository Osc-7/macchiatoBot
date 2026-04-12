from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import replace
from typing import Any, Dict, Literal, Optional

from agent_core.config import get_config
from agent_core.interfaces import AgentHooks, AgentRunInput, AgentRunResult
from system.automation import AutomationIPCClient, default_socket_path

from .client import FeishuClient
from .interactive_cards import (
    AGENT_REPLY_STREAM_ELEMENT_ID,
    AssistantReplyPhase,
    build_agent_reply_card_streaming_shell,
    build_agent_reply_markdown_card,
    build_tool_call_pending_card,
    build_tool_trace_card,
)
from .reply_dispatch import send_feishu_agent_reply
from .slash_commands import try_handle_slash_command

"""
Automation IPC Bridge for Feishu.

封装 AutomationIPCClient，提供面向飞书前端的简单消息发送接口。
支持斜杠指令（/clear、/usage、/session、/help）与 CLI 对齐。

FeishuPushForwarder：后台轮询 [out] 队列，将 inject_turn 等推送结果发回飞书。
"""


logger = logging.getLogger(__name__)

# 助手流式 PATCH：飞书单卡约 10 次/秒上限；过长的防抖会显得「一块一块」卡顿。
# 缩短定时刷新 + 累积够字符时立即刷新，在限流内尽量顺滑。
_FEISHU_STREAM_PATCH_DEBOUNCE_S = 0.12
_FEISHU_STREAM_PATCH_MIN_CHARS = 48

# 全局 push 转发器，供 ws_client 注册会话并启动
_feishu_push_forwarder: Optional["FeishuPushForwarder"] = None
_forwarder_lock = threading.Lock()


def get_feishu_push_forwarder() -> "FeishuPushForwarder":
    """获取或创建 Feishu push 转发器（单例）。"""
    global _feishu_push_forwarder
    with _forwarder_lock:
        if _feishu_push_forwarder is None:
            _feishu_push_forwarder = FeishuPushForwarder()
        return _feishu_push_forwarder


def register_feishu_push_session(
    session_id: str, chat_id: str, ttl_seconds: float = 300.0
) -> None:
    """注册需转发 push 的会话，供 FeishuPushForwarder 轮询并投递到对应 chat。"""
    get_feishu_push_forwarder().register(session_id, chat_id, ttl_seconds)


class AutomationDaemonUnavailable(RuntimeError):
    """当 automation daemon 未运行或 IPC 连接失败时抛出。"""


MSG_FEISHU_DAEMON_UNAVAILABLE = (
    "无法连接本地 automation 服务（macchiato agent），请确认已启动 automation_daemon。"
)


def format_feishu_processing_error(exc: BaseException) -> str:
    """将异常格式化为飞书用户可见的一行说明（避免过长）。"""
    msg = str(exc).strip() or type(exc).__name__
    if len(msg) > 500:
        msg = msg[:499] + "…"
    return f"处理消息时出错：{msg}"


async def send_feishu_error_notice(
    *,
    chat_id: str,
    text: str,
    timeout_seconds: float = 30.0,
) -> None:
    """向飞书会话发送一条用户可见的错误或状态提示。"""
    cid = (chat_id or "").strip()
    if not cid or not (text or "").strip():
        return
    try:
        client = FeishuClient(timeout_seconds=timeout_seconds)
        await client.send_text_message(chat_id=cid, text=text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "send_feishu_error_notice failed (chat_id=%s): %s", cid, exc
        )


async def try_handle_slash_command_via_ipc(
    *,
    session_id: str,
    text: str,
    socket_path: Optional[str] = None,
    timeout_seconds: float = 120.0,
    owner_id: str = "root",
    source: str = "feishu",
) -> Optional[str]:
    """
    尝试处理斜杠指令（/clear、/usage、/session、/help）。

    Returns:
        若为斜杠指令则返回回复文本，否则返回 None
    """
    client = AutomationIPCClient(
        owner_id=owner_id,
        source=source,
        socket_path=socket_path or default_socket_path(),
        timeout_seconds=timeout_seconds,
    )
    if not await client.ping():
        raise AutomationDaemonUnavailable(
            f"automation daemon is not reachable via IPC socket: {socket_path or default_socket_path()}"
        )
    await client.switch_session(session_id, create_if_missing=True)
    handled, reply = await try_handle_slash_command(client, text)
    return reply if handled else None


class FeishuIPCBridge:
    """飞书到 automation daemon 的 IPC 桥。"""

    def __init__(
        self,
        *,
        socket_path: Optional[str] = None,
        ipc_timeout_seconds: Optional[float] = None,
    ) -> None:
        self._socket_path = socket_path or default_socket_path()
        if ipc_timeout_seconds is not None:
            self._ipc_timeout_seconds = float(ipc_timeout_seconds)
        else:
            self._ipc_timeout_seconds = float(
                get_config().feishu.automation_ipc_timeout_seconds
            )

    async def send_message(
        self,
        *,
        session_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        owner_id: str = "root",
        source: str = "feishu",
    ) -> AgentRunResult:
        """
        将一条飞书消息转发给 automation daemon，并获取 Agent 的最终响应。

        同时基于 trace 事件在飞书侧输出「多轮推理中间状态」，与 CLI 的
        多轮工具调用展示保持一致（但不暴露思维链 reasoning token）。

        Args:
            session_id: Schedule Agent 会话 ID（已根据飞书会话映射）
            text: 输入的自然语言消息
            metadata: 附加到 AgentRunInput.metadata 的信息（可包含飞书 open_id/chat_id 等）
            owner_id: 逻辑用户 ID，目前主要用于日志标识
            source: 来源标识，固定为 "feishu"
        """
        client = AutomationIPCClient(
            owner_id=owner_id,
            source=source,
            socket_path=self._socket_path,
            timeout_seconds=self._ipc_timeout_seconds,
        )

        # 快速探测 daemon 是否在线
        if not await client.ping():
            raise AutomationDaemonUnavailable(
                f"automation daemon is not reachable via IPC socket: {self._socket_path}"
            )

        # 切换/创建对应会话
        await client.switch_session(session_id, create_if_missing=True)

        meta_dict: Dict[str, Any] = metadata or {}
        chat_id = str(meta_dict.get("feishu_chat_id") or "").strip()
        feishu_client: Optional[FeishuClient] = None
        fei = get_config().feishu
        if chat_id:
            # Open API 超时用 feishu.timeout_seconds；长回复/卡片单独保底 120s，避免与 IPC 长超时混用
            feishu_http_timeout = max(float(fei.timeout_seconds), 120.0)
            feishu_client = FeishuClient(timeout_seconds=feishu_http_timeout)

        tool_trace_cards_enabled = bool(chat_id and fei.tool_trace_cards_enabled)
        _rf = (fei.reply_format or "markdown_card").strip().lower()
        stream_reply = bool(
            chat_id
            and feishu_client
            and _rf == "markdown_card"
            and bool(getattr(fei, "assistant_reply_stream", True))
        )
        use_cardkit_stream = bool(
            stream_reply and bool(getattr(fei, "assistant_cardkit_stream", True))
        )
        # tool_call_id -> {name, arguments}；tool_result 时弹出
        pending_tool_args: Dict[str, Dict[str, Any]] = {}
        # tool_call 发出的「running」卡片对应 message_id，tool_result 时 PATCH 为结果卡（同一条消息）
        pending_tool_message_ids: Dict[str, str] = {}

        # 累积当前 LLM 调用的可见回复内容；stream_reply 时同一条卡片 PATCH 更新（类流式）。
        assistant_buffer: str = ""
        assistant_stream_mid: str = ""
        # 已成功 PATCH 到卡片上的 buffer 长度（用于按字符增量触发刷新）
        assistant_stream_patched_len: int = 0
        assistant_debounce_task: Optional[asyncio.Task] = None
        # CardKit 官方流式：card_id + PUT content（sequence 递增）；失败则 ck_fallback 走消息 PATCH
        ck_card_id: str = ""
        ck_seq: int = 0
        ck_fallback: bool = False

        def _ck_next_seq() -> int:
            nonlocal ck_seq
            ck_seq += 1
            return ck_seq

        async def _cancel_assistant_debounce() -> None:
            nonlocal assistant_debounce_task
            t = assistant_debounce_task
            assistant_debounce_task = None
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        async def _apply_assistant_reply_stream_card(
            *,
            reply_phase: AssistantReplyPhase = "streaming",
        ) -> None:
            nonlocal assistant_stream_mid
            if not feishu_client or not chat_id:
                return
            body = assistant_buffer
            if not body:
                return
            card = build_agent_reply_markdown_card(
                body, header_title="回复", reply_phase=reply_phase
            )
            try:
                if assistant_stream_mid:
                    await feishu_client.patch_interactive_card_message(
                        message_id=assistant_stream_mid, card=card
                    )
                else:
                    mid = await feishu_client.send_interactive_card(
                        chat_id=chat_id, card=card
                    )
                    if mid:
                        assistant_stream_mid = mid
            except Exception as exc:  # noqa: BLE001
                logger.warning("feishu assistant stream card failed: %s", exc)

        async def _apply_stream_patch_and_mark() -> None:
            nonlocal assistant_stream_patched_len
            await _apply_assistant_reply_stream_card(reply_phase="streaming")
            assistant_stream_patched_len = len(assistant_buffer)

        async def _ck_put_buffer() -> None:
            nonlocal assistant_stream_patched_len
            if not ck_card_id or not feishu_client:
                return
            text = assistant_buffer
            if not text:
                return
            if len(text) > 100_000:
                text = text[:100_000]
            await feishu_client.cardkit_put_streaming_text_content(
                card_id=ck_card_id,
                element_id=AGENT_REPLY_STREAM_ELEMENT_ID,
                content=text,
                sequence=_ck_next_seq(),
            )
            assistant_stream_patched_len = len(assistant_buffer)

        async def _ck_close_streaming_card(
            last_plain: str,
            *,
            close_kind: Literal["segment", "final"],
        ) -> None:
            nonlocal ck_card_id, ck_seq, assistant_stream_patched_len
            if not ck_card_id or not feishu_client:
                return
            text = last_plain or ""
            if len(text) > 100_000:
                text = text[:100_000]
            phase: AssistantReplyPhase = (
                "final" if close_kind == "final" else "segment"
            )
            # 仅 PATCH config 无法更新标题区 text_tag，流式壳会一直是 Streaming；需全量 PUT 卡片。
            card = build_agent_reply_markdown_card(
                text, header_title="回复", reply_phase=phase
            )
            cfg = card.setdefault("config", {})
            cfg["streaming_mode"] = False
            cfg.pop("streaming_config", None)
            await feishu_client.cardkit_replace_card_entity(
                card_id=ck_card_id,
                card=card,
                sequence=_ck_next_seq(),
            )
            ck_card_id = ""
            ck_seq = 0
            assistant_stream_patched_len = 0

        async def _ck_bootstrap() -> None:
            nonlocal ck_card_id, ck_fallback, ck_seq, assistant_stream_patched_len
            fc = feishu_client
            if not fc or not chat_id:
                return
            try:
                shell = build_agent_reply_card_streaming_shell(reply_phase="streaming")
                cid = await fc.create_cardkit_card_entity(card=shell)
                await fc.send_message_with_card_id(chat_id=chat_id, card_id=cid)
                ck_card_id = cid
                ck_seq = 0
                await _ck_put_buffer()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "feishu CardKit stream unavailable, fallback to message PATCH: %s",
                    exc,
                )
                ck_fallback = True
                ck_card_id = ""
                ck_seq = 0
                await _apply_stream_patch_and_mark()

        async def _flush_assistant_buffer_legacy() -> None:
            nonlocal assistant_buffer
            if not feishu_client or not chat_id:
                assistant_buffer = ""
                return
            text_out = assistant_buffer.strip()
            if not text_out:
                assistant_buffer = ""
                return
            try:
                await send_feishu_agent_reply(
                    client=feishu_client,
                    chat_id=chat_id,
                    output_text=text_out,
                    markdown_card_header_title="回复",
                    reply_phase="segment",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "failed to send feishu intermediate assistant message: %s", exc
                )
            finally:
                assistant_buffer = ""

        async def _end_assistant_segment_before_tools() -> None:
            """进入工具调用 / 下一轮 LLM 前，结束当前助手文本段在飞书侧的展示状态。"""
            nonlocal assistant_buffer, assistant_stream_mid, assistant_stream_patched_len, ck_card_id, ck_seq
            await _cancel_assistant_debounce()
            if not assistant_buffer.strip():
                assistant_buffer = ""
                assistant_stream_mid = ""
                assistant_stream_patched_len = 0
                ck_card_id = ""
                ck_seq = 0
                return
            if stream_reply:
                if use_cardkit_stream and not ck_fallback and ck_card_id:
                    await _ck_close_streaming_card(
                        assistant_buffer, close_kind="segment"
                    )
                    assistant_buffer = ""
                    assistant_stream_mid = ""
                    return
                if assistant_stream_mid:
                    await _apply_assistant_reply_stream_card(reply_phase="segment")
                else:
                    await _flush_assistant_buffer_legacy()
                    assistant_buffer = ""
                    assistant_stream_mid = ""
                    assistant_stream_patched_len = 0
                    return
                assistant_buffer = ""
                assistant_stream_mid = ""
                assistant_stream_patched_len = 0
            else:
                await _flush_assistant_buffer_legacy()

        async def _on_assistant_delta(delta: str) -> None:
            nonlocal assistant_buffer, assistant_debounce_task
            if not delta:
                return
            assistant_buffer += delta
            if not stream_reply or not feishu_client or not chat_id:
                return

            if use_cardkit_stream and not ck_fallback:
                if not ck_card_id:
                    await _ck_bootstrap()
                    return
                pending_chars = len(assistant_buffer) - assistant_stream_patched_len
                if pending_chars >= _FEISHU_STREAM_PATCH_MIN_CHARS:
                    await _cancel_assistant_debounce()
                    await _ck_put_buffer()
                    return

                async def _debounced_ck() -> None:
                    try:
                        await asyncio.sleep(_FEISHU_STREAM_PATCH_DEBOUNCE_S)
                        await _ck_put_buffer()
                    except asyncio.CancelledError:
                        pass

                await _cancel_assistant_debounce()
                assistant_debounce_task = asyncio.create_task(_debounced_ck())
                return

            if not assistant_stream_mid:
                await _apply_stream_patch_and_mark()
                return

            pending_chars = len(assistant_buffer) - assistant_stream_patched_len
            if pending_chars >= _FEISHU_STREAM_PATCH_MIN_CHARS:
                await _cancel_assistant_debounce()
                await _apply_stream_patch_and_mark()
                return

            async def _debounced() -> None:
                try:
                    await asyncio.sleep(_FEISHU_STREAM_PATCH_DEBOUNCE_S)
                    await _apply_stream_patch_and_mark()
                except asyncio.CancelledError:
                    pass

            await _cancel_assistant_debounce()
            assistant_debounce_task = asyncio.create_task(_debounced())

        async def _on_trace_event(evt: Dict[str, Any]) -> None:
            # 仅在有 chat_id 时才在飞书侧展示中间输出
            if not feishu_client or not chat_id:
                return
            evt_type = str(evt.get("type") or "")

            if evt_type == "tool_call" and tool_trace_cards_enabled:
                tcid = str(evt.get("tool_call_id") or "").strip()
                if tcid:
                    pending_tool_args[tcid] = {
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
                        mid = await feishu_client.send_interactive_card(
                            chat_id=chat_id, card=card_pending
                        )
                        if mid:
                            pending_tool_message_ids[tcid] = mid
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "failed to send feishu tool_call pending card (tool=%s): %s",
                            evt.get("name"),
                            exc,
                        )
                return

            if evt_type == "tool_result" and tool_trace_cards_enabled:
                tcid = str(evt.get("tool_call_id") or "").strip()
                meta = pending_tool_args.pop(tcid, None) if tcid else None
                msg_id = (
                    pending_tool_message_ids.pop(tcid, "") if tcid else ""
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
                            await feishu_client.patch_interactive_card_message(
                                message_id=msg_id, card=card
                            )
                        except Exception as patch_exc:  # noqa: BLE001
                            logger.warning(
                                "feishu patch tool card failed (tool=%s), send new: %s",
                                name,
                                patch_exc,
                            )
                            await feishu_client.send_interactive_card(
                                chat_id=chat_id, card=card
                            )
                    else:
                        await feishu_client.send_interactive_card(
                            chat_id=chat_id, card=card
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "failed to send feishu tool trace card (tool=%s): %s",
                        name,
                        exc,
                    )
                return

            # 当进入新一轮 LLM 调用时，若上一轮已经产生了可见回复内容，
            # 则先将其作为「中间输出」发到飞书，再开始下一轮累积。
            if evt_type == "llm_request":
                if assistant_buffer.strip():
                    await _end_assistant_segment_before_tools()
                return
            return

        agent_input = AgentRunInput(text=text, metadata=meta_dict)
        hooks = AgentHooks(
            on_assistant_delta=_on_assistant_delta,
            on_trace_event=_on_trace_event,
        )
        result = await client.run_turn(agent_input, hooks=hooks)

        await _cancel_assistant_debounce()
        out_text = (result.output_text or "").strip()
        if (
            use_cardkit_stream
            and not ck_fallback
            and feishu_client
            and chat_id
            and ck_card_id
            and out_text
        ):
            try:
                await _ck_close_streaming_card(out_text, close_kind="final")
                meta = dict(result.metadata)
                meta["feishu_skip_final_reply"] = True
                result = replace(result, metadata=meta)
            except Exception as exc:  # noqa: BLE001
                logger.warning("feishu CardKit final close failed: %s", exc)
        elif (
            stream_reply
            and feishu_client
            and chat_id
            and assistant_stream_mid
            and out_text
        ):
            try:
                card = build_agent_reply_markdown_card(
                    out_text, header_title="回复", reply_phase="final"
                )
                await feishu_client.patch_interactive_card_message(
                    message_id=assistant_stream_mid, card=card
                )
                meta = dict(result.metadata)
                meta["feishu_skip_final_reply"] = True
                result = replace(result, metadata=meta)
            except Exception as exc:  # noqa: BLE001
                logger.warning("feishu final assistant stream patch failed: %s", exc)

        return result


class FeishuPushForwarder:
    """后台轮询 [out] 队列，将 inject_turn 等推送结果发回飞书。

    与 CLI 的 _automation_notifier_loop 对齐：统一从 kernel [out] 队列取结果，
    按 session 投递到对应前端。飞书侧投递到 feishu_chat_id。
    """

    def __init__(
        self,
        *,
        poll_interval_seconds: float = 2.0,
        socket_path: Optional[str] = None,
        timeout_seconds: float = 30.0,
        session_ttl_seconds: float = 300.0,
    ) -> None:
        self._poll_interval = poll_interval_seconds
        self._socket_path = socket_path or default_socket_path()
        self._timeout = timeout_seconds
        # 会话注册有效期：subagent 等 inject_turn 可能远超 5 分钟才全部完成，
        # 成功转发推送后会在 _poll_once 内滑动续期，避免后续结果无法送达飞书。
        self._session_ttl_seconds = float(session_ttl_seconds)
        self._registry: Dict[str, tuple[str, float]] = {}  # session_id -> (chat_id, expiry_ts)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def register(
        self, session_id: str, chat_id: str, ttl_seconds: Optional[float] = None
    ) -> None:
        """注册会话，在 ttl_seconds 内轮询 [out] 队列并转发到 chat_id。"""
        if not session_id or not chat_id:
            return
        ttl = float(ttl_seconds) if ttl_seconds is not None else self._session_ttl_seconds
        expiry = time.time() + ttl
        with self._lock:
            self._registry[session_id] = (chat_id, expiry)

    def start(self) -> None:
        """启动后台轮询线程。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="feishu-push-forwarder",
                daemon=True,
            )
            self._thread.start()
            logger.info("FeishuPushForwarder started")

    def _run_loop(self) -> None:
        """轮询循环（在独立线程中运行，内部创建新事件循环）。"""
        while not self._stop.wait(timeout=self._poll_interval):
            try:
                asyncio.run(self._poll_once())
            except Exception as exc:
                logger.warning("FeishuPushForwarder poll_once failed: %s", exc)

    async def _poll_once(self) -> None:
        """单次轮询：对已注册会话执行 poll_push 并转发。"""
        now = time.time()
        with self._lock:
            to_poll = [
                (sid, chat_id)
                for sid, (chat_id, expiry) in list(self._registry.items())
                if expiry > now
            ]
            # 移除过期
            self._registry = {
                sid: (cid, exp)
                for sid, (cid, exp) in self._registry.items()
                if exp > now
            }
        if not to_poll:
            return
        try:
            client = AutomationIPCClient(
                owner_id="root",
                source="feishu",
                socket_path=self._socket_path,
                timeout_seconds=self._timeout,
            )
            if not await client.ping():
                return
            feishu_client = FeishuClient(timeout_seconds=self._timeout)
            for session_id, chat_id in to_poll:
                await client.switch_session(session_id, create_if_missing=False)
                results = await client.poll_push()
                sent_any = False
                for pr in results or []:
                    text = (pr.get("output_text") or "").strip()
                    if not text:
                        continue
                    try:
                        await feishu_client.send_text_message(
                            chat_id=chat_id, text=text
                        )
                        sent_any = True
                    except Exception as exc:
                        logger.warning(
                            "FeishuPushForwarder send to chat_id=%s failed: %s",
                            chat_id,
                            exc,
                        )
                # 有推送成功则续期，避免长任务在首条用户消息后固定 300s 过期、后续 inject 丢失
                if sent_any:
                    self.register(session_id, chat_id)
        except Exception as exc:
            logger.debug("FeishuPushForwarder _poll_once: %s", exc)
