"""Session manager for queue-driven Agent execution.

Manages ScheduleAgent instance lifecycles based on context_policy:
  - ephemeral:  New agent per task, destroyed after execution. No chat history persisted.
                Suitable for automated/scheduled tasks.
  - persistent: Agent reused per session_id across tasks. LongTermMemory loaded on creation.
                Suitable for interactive user sessions (CLI, social platforms).
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from schedule_agent.config import Config, get_config
from schedule_agent.core.tools import BaseTool

from .agent_task import ContextPolicy

logger = logging.getLogger(__name__)

# Lazy import to avoid circular dependency issues at module load time.
def _import_schedule_agent():
    from schedule_agent.core.agent import ScheduleAgent
    return ScheduleAgent


class SessionManager:
    """
    按 session_id 隔离 ScheduleAgent 实例。

    Usage::

        manager = SessionManager(config=config, tools_factory=lambda: get_default_tools(config))
        result = await manager.run_task(task)
        await manager.close_all()
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        tools_factory: Optional[Callable[[], List[BaseTool]]] = None,
    ):
        self._config = config or get_config()
        self._tools_factory = tools_factory
        self._sessions: Dict[str, object] = {}

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def run_task(
        self,
        session_id: str,
        instruction: str,
        context_policy: ContextPolicy,
        on_trace_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> str:
        """
        执行一条任务指令并返回 Agent 响应。

        - ephemeral: 新建临时 Agent，执行完立即关闭，不保留对话历史。
        - persistent: 按 session_id 复用 Agent 实例，保留对话历史和长期记忆。
        """
        if context_policy == ContextPolicy.EPHEMERAL:
            return await self._run_ephemeral(instruction, on_trace_event=on_trace_event)
        else:
            return await self._run_persistent(session_id, instruction, on_trace_event=on_trace_event)

    async def close_session(self, session_id: str) -> None:
        """关闭并移除指定的 persistent session。"""
        agent = self._sessions.pop(session_id, None)
        if agent is not None:
            await agent.close()
            logger.debug("Closed persistent session: %s", session_id)

    async def close_all(self) -> None:
        """关闭所有 persistent sessions，释放资源。"""
        session_ids = list(self._sessions.keys())
        for session_id in session_ids:
            await self.close_session(session_id)

    def active_sessions(self) -> List[str]:
        """返回当前所有活跃的 persistent session_id 列表。"""
        return list(self._sessions.keys())

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _create_agent(self):
        ScheduleAgent = _import_schedule_agent()
        tools = self._tools_factory() if self._tools_factory else []
        agent = ScheduleAgent(
            config=self._config,
            tools=tools,
            max_iterations=self._config.agent.max_iterations,
            timezone=self._config.time.timezone,
        )
        return agent

    async def _run_ephemeral(
        self,
        instruction: str,
        on_trace_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> str:
        """每次新建 Agent，执行完立即关闭。不持久化任何对话记忆。"""
        agent = self._create_agent()
        try:
            result = await agent.process_input(instruction, on_trace_event=on_trace_event)
            return result
        finally:
            await agent.close()

    async def _run_persistent(
        self,
        session_id: str,
        instruction: str,
        on_trace_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> str:
        """复用同一 Agent 实例，保持对话上下文。启动时会自动加载 LongTermMemory（由 Agent 内部处理）。"""
        if session_id not in self._sessions:
            agent = self._create_agent()
            self._sessions[session_id] = agent
            logger.debug("Created persistent session: %s", session_id)

        agent = self._sessions[session_id]
        return await agent.process_input(instruction, on_trace_event=on_trace_event)
