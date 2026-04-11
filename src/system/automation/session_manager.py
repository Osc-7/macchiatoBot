"""Session manager for queue-driven Agent execution.

Manages AgentCore instance lifecycles based on context_policy:
  - ephemeral:  New agent per task, destroyed after execution. No chat history persisted.
                Suitable for automated/scheduled tasks.
  - persistent: Agent reused per session_id across tasks. LongTermMemory loaded on creation.
                Suitable for interactive user sessions (CLI, social platforms).
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent_core.config import Config, get_config
from agent_core.adapters import CoreSessionAdapter
from agent_core.interfaces import AgentHooks, AgentRunInput, CoreSession, RunTurnCommand
from agent_core.kernel_interface import CoreProfile
from agent_core.tools import BaseTool

from .agent_task import ContextPolicy

logger = logging.getLogger(__name__)


def _parse_session_id(session_id: str) -> Tuple[str, str]:
    """从 session_id 解析 source 和 user_id。如 cron:daily_memory_sync -> (cron, daily_memory_sync)。"""
    if ":" in session_id:
        parts = session_id.split(":", 1)
        return (parts[0].strip() or "schedule", parts[1].strip() or "root")
    return ("schedule", session_id.strip() or "root")


# Lazy import to avoid circular dependency issues at module load time.
def _import_schedule_agent():
    from agent_core.agent import AgentCore

    return AgentCore


class SessionManager:
    """
    按 session_id 隔离 AgentCore 实例。

    Usage::

        from system.tools import get_default_tools
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
        self._sessions: Dict[str, CoreSession] = {}

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
        command = RunTurnCommand(
            session_id=session_id, input=AgentRunInput(text=instruction)
        )
        if context_policy == ContextPolicy.EPHEMERAL:
            return await self._run_ephemeral(command, on_trace_event=on_trace_event)
        return await self._run_persistent(command, on_trace_event=on_trace_event)

    async def close_session(self, session_id: str) -> None:
        """关闭并移除指定的 persistent session。"""
        session = self._sessions.pop(session_id, None)
        if session is not None:
            raw = getattr(session, "raw_agent", None)
            if raw is not None and hasattr(raw, "_session_logger"):
                sl = getattr(raw, "_session_logger", None)
                if sl is not None and hasattr(sl, "on_core_end"):
                    try:
                        maybe = sl.on_core_end(stats=None)
                        if inspect.isawaitable(maybe):
                            await maybe
                    except Exception:
                        pass
            await session.close()
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

    def _create_agent(
        self,
        session_id: str,
        *,
        session_logger: Optional[Any] = None,
    ):
        AgentCore = _import_schedule_agent()
        tools = self._tools_factory() if self._tools_factory else []
        source, user_id = _parse_session_id(session_id)
        if source == "shuiyuan":
            profile = CoreProfile.for_shuiyuan(
                dialog_window_id=user_id,
                tools_config=self._config.tools,
            )
        else:
            profile = CoreProfile.full_from_config(
                self._config,
                frontend_id=source,
                dialog_window_id=user_id,
            )
        agent = AgentCore(
            config=self._config,
            tools=tools,
            max_iterations=self._config.agent.max_iterations,
            timezone=self._config.time.timezone,
            session_logger=session_logger,
            user_id=user_id,
            source=source,
            core_profile=profile,
        )
        return agent

    def _create_session_logger(self, session_id: str) -> Optional[Any]:
        """为 SessionManager 路径创建 CoreLifecycleLogger，补齐定时任务等 trace。"""
        log_cfg = getattr(self._config, "logging", None)
        if not log_cfg or not getattr(log_cfg, "enable_session_log", True):
            return None
        try:
            from system.kernel.core_logger import CoreLifecycleLogger

            log_dir = getattr(log_cfg, "session_log_dir", "./logs/sessions")
            enable_detailed = getattr(log_cfg, "enable_detailed_log", False)
            max_sp_len = getattr(log_cfg, "max_system_prompt_log_len", 2000)
            source, user_id = _parse_session_id(session_id)
            logger_obj = CoreLifecycleLogger(
                base_dir=log_dir,
                source=source,
                user_id=user_id,
                session_id=session_id,
                enable_detailed_log=enable_detailed,
                max_system_prompt_log_len=max_sp_len,
            )
            logger_obj.on_core_start(profile=None)
            return logger_obj
        except Exception as exc:
            logger.warning(
                "SessionManager: CoreLifecycleLogger creation failed for session=%s: %s",
                session_id,
                exc,
            )
            return None

    def _create_session(self, session_id: str) -> CoreSession:
        session_logger = self._create_session_logger(session_id)
        agent = self._create_agent(session_id, session_logger=session_logger)
        return CoreSessionAdapter(agent)

    async def _run_ephemeral(
        self,
        command: RunTurnCommand,
        on_trace_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> str:
        """每次新建 Agent，执行完立即关闭。不持久化任何对话记忆。"""
        session = self._create_session(command.session_id)
        try:
            activate = getattr(session, "activate_session", None)
            if callable(activate):
                maybe = activate(command.session_id)
                if inspect.isawaitable(maybe):
                    await maybe
            run_result = await session.run_turn(
                command.input,
                hooks=AgentHooks(on_trace_event=on_trace_event),
            )
            return run_result.output_text
        finally:
            # 关闭 session 时确保 core_logger 写入 core_end
            raw = getattr(session, "raw_agent", None)
            if raw is not None and hasattr(raw, "_session_logger"):
                sl = getattr(raw, "_session_logger", None)
                if sl is not None and hasattr(sl, "on_core_end"):
                    try:
                        maybe = sl.on_core_end(stats=None)
                        if inspect.isawaitable(maybe):
                            await maybe
                    except Exception:
                        pass
            await session.close()

    async def _run_persistent(
        self,
        command: RunTurnCommand,
        on_trace_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> str:
        """复用同一 Agent 实例，保持对话上下文。启动时会自动加载 LongTermMemory（由 Agent 内部处理）。"""
        session_id = command.session_id
        if session_id not in self._sessions:
            session = self._create_session(session_id)
            activate = getattr(session, "activate_session", None)
            if callable(activate):
                maybe = activate(session_id)
                if inspect.isawaitable(maybe):
                    await maybe
            self._sessions[session_id] = session
            logger.debug("Created persistent session: %s", session_id)

        run_result = await self._sessions[session_id].run_turn(
            command.input,
            hooks=AgentHooks(on_trace_event=on_trace_event),
        )
        return run_result.output_text
