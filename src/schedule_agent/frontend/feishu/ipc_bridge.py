from __future__ import annotations

"""
Automation IPC Bridge for Feishu.

封装 AutomationIPCClient，提供面向飞书前端的简单消息发送接口。
"""

import json
import logging
from typing import Any, Dict, Optional

from schedule_agent.automation import AutomationIPCClient, default_socket_path
from schedule_agent.core.interfaces import AgentHooks, AgentRunInput, AgentRunResult

from .client import FeishuClient

logger = logging.getLogger(__name__)


class AutomationDaemonUnavailable(RuntimeError):
    """当 automation daemon 未运行或 IPC 连接失败时抛出。"""


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
                logger.warning("failed to send feishu intermediate assistant message: %s", exc)
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

