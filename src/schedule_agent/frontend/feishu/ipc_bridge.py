from __future__ import annotations

"""
Automation IPC Bridge for Feishu.

封装 AutomationIPCClient，提供面向飞书前端的简单消息发送接口。
"""

from typing import Any, Dict, Optional

from schedule_agent.automation import AutomationIPCClient, default_socket_path
from schedule_agent.core.interfaces import AgentHooks, AgentRunInput, AgentRunResult


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

        agent_input = AgentRunInput(text=text, metadata=metadata or {})
        hooks = AgentHooks()
        result = await client.run_turn(agent_input, hooks=hooks)
        return result

