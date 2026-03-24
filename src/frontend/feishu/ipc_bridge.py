from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional

from system.automation import AutomationIPCClient, default_socket_path
from agent_core.interfaces import AgentHooks, AgentRunInput, AgentRunResult

from .client import FeishuClient
from .slash_commands import try_handle_slash_command

"""
Automation IPC Bridge for Feishu.

封装 AutomationIPCClient，提供面向飞书前端的简单消息发送接口。
支持斜杠指令（/clear、/usage、/session、/help）与 CLI 对齐。

FeishuPushForwarder：后台轮询 [out] 队列，将 inject_turn 等推送结果发回飞书。
"""


logger = logging.getLogger(__name__)

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
        timeout_seconds: float = 120.0,
    ) -> None:
        self._socket_path = socket_path or default_socket_path()
        self._timeout_seconds = float(timeout_seconds)

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
            timeout_seconds=self._timeout_seconds,
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
        if chat_id:
            # 与最终回复共用同一个 FeishuClient，避免重复创建 token cache
            feishu_client = FeishuClient(timeout_seconds=self._timeout_seconds)

        # 累积当前 LLM 调用的可见回复内容，用于在多轮工具调用时输出「中间轮次」的
        # Agent 自然语言说明（不含思维链，不含工具内部细节）。
        assistant_buffer: str = ""

        async def _flush_assistant_buffer() -> None:
            nonlocal assistant_buffer
            if not feishu_client or not chat_id:
                assistant_buffer = ""
                return
            text_out = assistant_buffer.strip()
            if not text_out:
                return
            try:
                await feishu_client.send_text_message(chat_id=chat_id, text=text_out)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "failed to send feishu intermediate assistant message: %s", exc
                )
            finally:
                assistant_buffer = ""

        async def _on_assistant_delta(delta: str) -> None:
            nonlocal assistant_buffer
            if not delta:
                return
            assistant_buffer += delta

        async def _on_trace_event(evt: Dict[str, Any]) -> None:
            # 仅在有 chat_id 时才在飞书侧展示中间输出
            if not feishu_client or not chat_id:
                return
            evt_type = str(evt.get("type") or "")

            # 当进入新一轮 LLM 调用时，若上一轮已经产生了可见回复内容，
            # 则先将其作为「中间输出」发到飞书，再开始下一轮累积。
            if evt_type == "llm_request":
                if assistant_buffer.strip():
                    await _flush_assistant_buffer()
                return
            # 对于 tool_call / tool_result 等事件，移动端可以省略具体细节，
            # 因此这里不再单独输出，只依赖 Agent 自然语言说明。
            return

        agent_input = AgentRunInput(text=text, metadata=meta_dict)
        hooks = AgentHooks(
            on_assistant_delta=_on_assistant_delta,
            on_trace_event=_on_trace_event,
        )
        result = await client.run_turn(agent_input, hooks=hooks)
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
