"""In-process Automation gateway for channel -> core dispatch."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Dict, Optional

from .session_registry import SessionRegistry
from schedule_agent.core.interfaces import (
    AgentHooks,
    AgentRunInput,
    AgentRunResult,
    CoreSession,
    ExpireSessionCommand,
    InjectMessageCommand,
    RunTurnCommand,
    merge_run_metadata,
)

logger = logging.getLogger(__name__)


CoreSessionFactory = Callable[[str], CoreSession | Awaitable[CoreSession]]


@dataclass
class SessionCutPolicy:
    idle_timeout_minutes: int = 30
    daily_cutoff_hour: int = 4


class AutomationCoreGateway:
    """
    进程内 Automation 网关。

    将 CLI / 其他 channel 的输入先转成 Automation Command，再下发到 CoreSession。
    """

    def __init__(
        self,
        core_session: CoreSession,
        *,
        session_id: str = "cli:default",
        policy: Optional[SessionCutPolicy] = None,
        session_factory: Optional[CoreSessionFactory] = None,
        owner_id: str = "root",
        source: str = "cli",
        session_registry: Optional[SessionRegistry] = None,
    ):
        self._sessions: Dict[str, CoreSession] = {session_id: core_session}
        self._owned_sessions: set[str] = set()
        self._active_session_id = session_id
        self._owner_id = owner_id.strip() or "root"
        self._source = source.strip() or "cli"
        self._policy = policy or SessionCutPolicy()
        now = datetime.now()
        self._last_activity: Dict[str, datetime] = {session_id: now}
        self._session_factory = session_factory
        self._session_lock = asyncio.Lock()
        self._session_registry = session_registry or SessionRegistry()
        self._session_registry.upsert_session(self._owner_id, self._source, session_id)

    @property
    def config(self):
        # 兼容 interactive.py 现有读取方式
        return getattr(self._active_session(), "config", None)

    @property
    def raw_core_session(self) -> CoreSession:
        return self._active_session()

    @property
    def active_session_id(self) -> str:
        return self._active_session_id

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def source(self) -> str:
        return self._source

    def list_sessions(self) -> list[str]:
        seen = set(self._sessions.keys())
        for sid in self._session_registry.list_sessions(self._owner_id, self._source):
            seen.add(sid)
        return sorted(seen)

    async def ensure_session(self, session_id: str, *, create_if_missing: bool = True) -> bool:
        """
        确保某个 session 已可用，但不改变当前 active_session_id。

        Returns:
            是否为新创建的 session
        """
        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session_id 不能为空")
        existed_any = session_id in self._sessions or self._session_registry.session_exists(
            self._owner_id, self._source, session_id
        )
        if session_id not in self._sessions:
            if not create_if_missing and not existed_any:
                raise KeyError(f"session not found: {session_id}")
            await self._create_session(session_id)
        return not existed_any

    async def switch_session(self, session_id: str, *, create_if_missing: bool = True) -> bool:
        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session_id 不能为空")
        existed_any = session_id in self._sessions or self._session_registry.session_exists(
            self._owner_id, self._source, session_id
        )
        created = False
        if session_id not in self._sessions:
            if not create_if_missing:
                if not existed_any:
                    raise KeyError(f"session not found: {session_id}")
            await self._create_session(session_id)
            created = not existed_any
        self._active_session_id = session_id
        self.mark_activity(session_id)
        return created

    async def run_turn(
        self,
        agent_input: AgentRunInput,
        hooks: AgentHooks | None = None,
    ) -> AgentRunResult:
        command = RunTurnCommand(session_id=self._active_session_id, input=agent_input)
        result = await self._dispatch_run_turn(command, hooks=hooks)
        self.mark_activity(command.session_id)
        return result

    async def inject_message(
        self,
        command: InjectMessageCommand,
        hooks: AgentHooks | None = None,
    ) -> AgentRunResult:
        result = await self._dispatch_run_turn(
            RunTurnCommand(session_id=command.session_id, input=command.input, metadata=command.metadata),
            hooks=hooks,
        )
        self.mark_activity(command.session_id)
        return result

    def mark_activity(self, session_id: Optional[str] = None) -> None:
        sid = session_id or self._active_session_id
        self._last_activity[sid] = datetime.now()

    def should_expire_session(self, session_id: Optional[str] = None) -> bool:
        sid = session_id or self._active_session_id
        now = datetime.now()
        last_activity = self._last_activity.get(sid, now)
        idle_seconds = (now - last_activity).total_seconds()
        if idle_seconds >= self._policy.idle_timeout_minutes * 60:
            return True
        if last_activity.date() < now.date() and now.hour >= self._policy.daily_cutoff_hour:
            return True
        if last_activity.date() == now.date() and last_activity.hour < self._policy.daily_cutoff_hour <= now.hour:
            return True
        return False

    async def expire_session(self, reason: str = "session_expire", *, session_id: Optional[str] = None) -> None:
        sid = session_id or self._active_session_id
        command = ExpireSessionCommand(session_id=sid, reason=reason)
        await self._dispatch_expire(command)
        self.mark_activity(sid)

    async def expire_session_if_needed(self, reason: str = "session_expire") -> bool:
        sid = self._active_session_id
        if not self.should_expire_session(sid):
            return False
        await self.expire_session(reason=reason, session_id=sid)
        return True

    async def finalize_session(self):
        return await self._active_session().finalize_session()

    def reset_session(self) -> None:
        sid = self._active_session_id
        self._active_session().reset_session()
        self.mark_activity(sid)

    async def clear_context_for_session(self, session_id: str) -> None:
        session = await self._get_or_create_session(session_id)
        clear_fn = getattr(session, "clear_context", None)
        if callable(clear_fn):
            clear_fn()

    def clear_context(self) -> None:
        clear_fn = getattr(self._active_session(), "clear_context", None)
        if callable(clear_fn):
            clear_fn()

    def get_token_usage(self, session_id: Optional[str] = None) -> dict:
        sid = session_id or self._active_session_id
        session = self._sessions.get(sid)
        if session is None:
            return {}
        fn = getattr(session, "get_token_usage", None)
        if callable(fn):
            return fn()
        return {}

    def get_turn_count(self, session_id: Optional[str] = None) -> int:
        sid = session_id or self._active_session_id
        session = self._sessions.get(sid)
        if session is None:
            return 0
        state = session.get_session_state()
        return state.turn_count

    async def close(self) -> None:
        for session_id in list(self._owned_sessions):
            session = self._sessions.get(session_id)
            if session is None:
                continue
            try:
                await session.close()
            except Exception as exc:
                logger.warning("close owned session failed (session_id=%s): %s", session_id, exc)
            finally:
                self._sessions.pop(session_id, None)
                self._last_activity.pop(session_id, None)
                self._owned_sessions.discard(session_id)
        self._session_registry.close()

    async def _dispatch_run_turn(
        self,
        command: RunTurnCommand,
        hooks: AgentHooks | None = None,
    ) -> AgentRunResult:
        session = await self._get_or_create_session(command.session_id)
        merged_metadata = merge_run_metadata(
            session_id=command.session_id,
            input_metadata=command.input.metadata,
            command_metadata=command.metadata,
        )
        agent_input = AgentRunInput(text=command.input.text, metadata=merged_metadata)
        return await session.run_turn(agent_input, hooks=hooks)

    async def _dispatch_expire(self, command: ExpireSessionCommand) -> None:
        session = await self._get_or_create_session(command.session_id)
        try:
            await session.finalize_session()
        except Exception as exc:
            logger.warning(
                "finalize_session failed during session expire (session_id=%s, reason=%s): %s",
                command.session_id,
                command.reason,
                exc,
            )
        finally:
            session.reset_session()

    def _active_session(self) -> CoreSession:
        session = self._sessions.get(self._active_session_id)
        if session is None:
            raise RuntimeError(f"active session not found: {self._active_session_id}")
        return session

    async def _get_or_create_session(self, session_id: str) -> CoreSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        return await self._create_session(session_id)

    async def _create_session(self, session_id: str) -> CoreSession:
        if self._session_factory is None:
            raise KeyError(f"session not found: {session_id}")
        async with self._session_lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
            created = self._session_factory(session_id)
            session = await created if inspect.isawaitable(created) else created
            activate = getattr(session, "activate_session", None)
            if callable(activate):
                maybe = activate(session_id)
                if inspect.isawaitable(maybe):
                    await maybe
            self._sessions[session_id] = session
            self._owned_sessions.add(session_id)
            self._last_activity[session_id] = datetime.now()
            self._session_registry.upsert_session(self._owner_id, self._source, session_id)
            return session
