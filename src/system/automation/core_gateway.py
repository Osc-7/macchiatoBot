"""In-process Automation gateway for channel -> core dispatch."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, Optional

from .session_registry import SessionRegistry
from agent_core.interfaces import (
    AgentHooks,
    AgentRunInput,
    AgentRunResult,
    CoreSession,
    ExpireSessionCommand,
    InjectMessageCommand,
    RunTurnCommand,
    merge_run_metadata,
)

if TYPE_CHECKING:
    from system.kernel import KernelScheduler

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

    支持两种运行模式：
    1. 直接模式（默认）：直接 await CoreSession.run_turn()，保持原有行为。
    2. Kernel 调度模式：通过 attach_scheduler() 挂载 KernelScheduler，
       将请求投入 InputQueue，由 Scheduler 异步分发，支持跨 session 真并发
       和"乱序完成精准路由"（OutputRouter）。

    IPC 协议（AutomationIPCServer）和外部接口完全不变。
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
        kernel_scheduler: Optional["KernelScheduler"] = None,
    ):
        self._kernel_scheduler: Optional["KernelScheduler"] = kernel_scheduler
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
        # 在 upsert_session（会重置 is_expired=0）之前先记录过期状态，供 activate_primary_session 使用
        self._initial_session_was_expired: bool = self._session_registry.is_expired(
            self._owner_id, self._source, session_id
        )
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

    async def activate_primary_session(self) -> None:
        """
        激活主会话，根据创建时记录的 is_expired 状态决定是否重放历史消息。

        用于取代调用方直接调用 core_session.activate_session(session_id)，
        确保过期会话以空上下文启动，而非全量重放历史。
        """
        session = self._active_session()
        activate = getattr(session, "activate_session", None)
        if not callable(activate):
            return
        replay_limit: Optional[int] = 0 if self._initial_session_was_expired else None
        logger.info(
            "activate_primary_session: session_id=%s, was_expired=%s, replay_limit=%s",
            self._active_session_id,
            self._initial_session_was_expired,
            replay_limit,
        )
        maybe = activate(self._active_session_id, replay_messages_limit=replay_limit)
        if inspect.isawaitable(maybe):
            await maybe

    def list_sessions(self) -> list[str]:
        seen = set(self._sessions.keys())
        if self._kernel_scheduler is not None:
            try:
                seen.update(self._kernel_scheduler.core_pool.list_sessions())
            except Exception:
                pass
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
        if self._kernel_scheduler is not None:
            try:
                existed_any = existed_any or self._kernel_scheduler.core_pool.has_session(session_id)
            except Exception:
                pass
        if session_id not in self._sessions:
            if not create_if_missing and not existed_any:
                raise KeyError(f"session not found: {session_id}")
            # scheduler 模式下，CorePool 会在首个请求时懒加载，不强制创建本地 CoreSession。
            if self._kernel_scheduler is None:
                await self._create_session(session_id)
            else:
                self._session_registry.upsert_session(self._owner_id, self._source, session_id)
                self._last_activity[session_id] = datetime.now()
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

    def attach_scheduler(self, scheduler: "KernelScheduler") -> None:
        """
        挂载 KernelScheduler，启用异步队列调度模式。

        挂载后，run_turn() 和 inject_message() 会将请求投入 InputQueue，
        由 Scheduler 异步分发（create_task 真并发），通过 OutputRouter 精准路由结果。

        不挂载时（默认）保持原有直接调用 CoreSession.run_turn() 的行为，零感知迁移。
        """
        self._kernel_scheduler = scheduler

    async def run_turn(
        self,
        agent_input: AgentRunInput,
        hooks: AgentHooks | None = None,
    ) -> AgentRunResult:
        if self._kernel_scheduler is not None:
            return await self._run_turn_via_scheduler(
                self._active_session_id, agent_input, hooks
            )
        command = RunTurnCommand(session_id=self._active_session_id, input=agent_input)
        result = await self._dispatch_run_turn(command, hooks=hooks)
        self.mark_activity(command.session_id)
        return result

    async def _run_turn_via_scheduler(
        self,
        session_id: str,
        agent_input: AgentRunInput,
        hooks: AgentHooks | None = None,
    ) -> AgentRunResult:
        """
        通过 KernelScheduler 提交请求并等待结果。

        使用 OutputRouter（Future）机制：submit() 返回 Future，await 等待结果。
        不同 session 的请求并发执行，先完成先返回（乱序完成精准路由）。
        """
        from agent_core.kernel_interface import KernelRequest

        metadata = dict(agent_input.metadata)
        metadata.setdefault("source", self._source)
        metadata.setdefault("user_id", self._owner_id)
        if hooks is not None:
            metadata["_hooks"] = hooks

        profile = metadata.pop("_core_profile", None)
        request = KernelRequest.create(
            text=agent_input.text,
            session_id=session_id,
            frontend_id=self._source,
            metadata=metadata,
            profile=profile,
        )
        future = await self._kernel_scheduler.submit(request)
        result: AgentRunResult = await future
        self.mark_activity(session_id)
        return result

    async def inject_message(
        self,
        command: InjectMessageCommand,
        hooks: AgentHooks | None = None,
    ) -> AgentRunResult:
        if self._kernel_scheduler is not None:
            return await self._run_turn_via_scheduler(
                command.session_id,
                command.input,
                hooks=hooks,
            )
        result = await self._dispatch_run_turn(
            RunTurnCommand(session_id=command.session_id, input=command.input, metadata=command.metadata),
            hooks=hooks,
        )
        self.mark_activity(command.session_id)
        return result

    def mark_activity(self, session_id: Optional[str] = None) -> None:
        sid = session_id or self._active_session_id
        now = datetime.now()
        self._last_activity[sid] = now
        self._session_registry.upsert_session(self._owner_id, self._source, sid)

    def should_expire_session(self, session_id: Optional[str] = None) -> bool:
        sid = session_id or self._active_session_id
        if self._session_registry.is_expired(self._owner_id, self._source, sid):
            return False
        now = datetime.now()
        last_activity = self._last_activity.get(sid)
        if last_activity is None:
            registry_ts = self._session_registry.get_updated_at(self._owner_id, self._source, sid)
            last_activity = registry_ts or now
            self._last_activity[sid] = last_activity
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
        if self._kernel_scheduler is not None:
            try:
                await self._kernel_scheduler.core_pool.evict(sid)
            except Exception as exc:
                logger.warning("evict session failed (session_id=%s, reason=%s): %s", sid, reason, exc)
            self._session_registry.mark_expired(self._owner_id, self._source, sid)
            self._last_activity[sid] = datetime.now()
            return
        # 未加载到内存的冷会话不做 finalize（避免重放整段历史再重复摘要）；
        # 仅标记为 expired，等待下次显式激活后重新计时。
        if sid not in self._sessions:
            self._session_registry.mark_expired(self._owner_id, self._source, sid)
            self._last_activity[sid] = datetime.now()
            return
        command = ExpireSessionCommand(session_id=sid, reason=reason)
        await self._dispatch_expire(command)
        self._session_registry.mark_expired(self._owner_id, self._source, sid)

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
        if self._kernel_scheduler is not None:
            entry = self._kernel_scheduler.core_pool.get_entry(session_id)
            if entry is not None:
                clear_fn = getattr(entry.agent, "clear_context", None)
                if callable(clear_fn):
                    clear_fn()
                return
        session = await self._get_or_create_session(session_id)
        clear_fn = getattr(session, "clear_context", None)
        if callable(clear_fn):
            clear_fn()

    def clear_context(self) -> None:
        if self._kernel_scheduler is not None:
            entry = self._kernel_scheduler.core_pool.get_entry(self._active_session_id)
            if entry is not None:
                clear_fn = getattr(entry.agent, "clear_context", None)
                if callable(clear_fn):
                    clear_fn()
                return
        clear_fn = getattr(self._active_session(), "clear_context", None)
        if callable(clear_fn):
            clear_fn()

    _DEFAULT_USAGE = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "call_count": 0,
        "cost_yuan": 0.0,
    }

    def get_token_usage(self, session_id: Optional[str] = None) -> dict:
        sid = session_id or self._active_session_id
        if self._kernel_scheduler is not None:
            entry = self._kernel_scheduler.core_pool.get_entry(sid)
            if entry is not None:
                fn = getattr(entry.agent, "get_token_usage", None)
                if callable(fn):
                    result = fn()
                    if isinstance(result, dict):
                        return {**self._DEFAULT_USAGE, **result}
            return dict(self._DEFAULT_USAGE)
        session = self._sessions.get(sid)
        if session is None:
            return dict(self._DEFAULT_USAGE)
        fn = getattr(session, "get_token_usage", None)
        if callable(fn):
            result = fn()
            if isinstance(result, dict):
                return {**self._DEFAULT_USAGE, **result}
        return dict(self._DEFAULT_USAGE)

    def get_turn_count(self, session_id: Optional[str] = None) -> int:
        sid = session_id or self._active_session_id
        if self._kernel_scheduler is not None:
            entry = self._kernel_scheduler.core_pool.get_entry(sid)
            if entry is None:
                return 0
            fn = getattr(entry.agent, "get_turn_count", None)
            if callable(fn):
                try:
                    return int(fn())
                except Exception:
                    return 0
            return 0
        session = self._sessions.get(sid)
        if session is None:
            return 0
        state = session.get_session_state()
        return state.turn_count

    async def delete_session(self, session_id: str) -> bool:
        """删除指定会话。

        - 删除 ChatHistoryDB 中该 session 的历史消息（如可用）
        - 关闭并移除内存中的 CoreSession（如有）
        - 从 SessionRegistry 中删除该会话记录
        - 不删除长期记忆（LongTermMemory），仅清理对话历史

        为避免当前交互状态混乱，不允许删除当前 active_session。
        """
        sid = (session_id or "").strip()
        if not sid:
            return False
        if sid == self._active_session_id:
            # 不直接删除当前活跃会话，避免前端仍在使用时状态不一致。
            return False

        existed_in_memory = sid in self._sessions
        existed_in_registry = self._session_registry.session_exists(self._owner_id, self._source, sid)
        # 既不在内存也不在注册表中，视为不存在的会话，直接返回失败，避免误报“删除成功”。
        if not existed_in_memory and not existed_in_registry:
            return False

        # 确保有一个 CoreSession 用于执行历史删除；对于未加载的冷会话，通过 session_factory 创建临时实例。
        session = self._sessions.get(sid)
        created_temp = False
        if session is None and existed_in_registry and self._session_factory is not None:
            created = self._session_factory(sid)
            session = await created if inspect.isawaitable(created) else created
            created_temp = True

        async def _close_session_if_needed(target: CoreSession | None, *, temp: bool) -> None:
            if target is None:
                return
            close = getattr(target, "close", None)
            if not callable(close):
                return
            try:
                maybe = close()
                if inspect.isawaitable(maybe):
                    await maybe
            except Exception as exc:
                if temp:
                    logger.warning("close temp session failed during delete (session_id=%s): %s", sid, exc)
                else:
                    logger.warning("close session failed during delete (session_id=%s): %s", sid, exc)

        # 没有可用 CoreSession 时，无法保证历史已被删除；直接失败，避免“元数据删除但历史残留”。
        if session is None:
            logger.warning("delete_session aborted: no core session available (session_id=%s)", sid)
            return False

        # 优先删除 ChatHistoryDB 中该 session 的历史；仅当删除动作成功时继续删除注册表元数据。
        delete_history = getattr(session, "delete_session_history", None)
        if not callable(delete_history):
            logger.warning("delete_session aborted: delete_session_history is unavailable (session_id=%s)", sid)
            if created_temp:
                await _close_session_if_needed(session, temp=True)
            return False
        try:
            maybe = delete_history(sid)
            if inspect.isawaitable(maybe):
                await maybe
        except Exception as exc:
            logger.warning("delete_session_history failed (session_id=%s): %s", sid, exc)
            if created_temp:
                await _close_session_if_needed(session, temp=True)
            return False

        # 如果是常驻在 Gateway 中的会话，需要从管理结构中移除并关闭。
        if sid in self._sessions:
            owned = sid in self._owned_sessions
            session = self._sessions.pop(sid, None)
            self._last_activity.pop(sid, None)
            if owned:
                self._owned_sessions.discard(sid)
            await _close_session_if_needed(session, temp=False)
        elif created_temp:
            # 临时创建的会话只用于清理历史，最后显式关闭但不注册到 Gateway。
            await _close_session_if_needed(session, temp=True)

        # 删除注册表中的元数据记录
        self._session_registry.delete_session(self._owner_id, self._source, sid)
        return True

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
                "finalize_session failed during session expire (session_id=%s, reason=%s, exc_type=%s): %s",
                command.session_id,
                command.reason,
                type(exc).__name__,
                exc,
            )
        finally:
            session.reset_session()
            # reset_session 可能生成临时随机 session_id；为保证路由键稳定，
            # 过期后立即回绑到原 session_id。
            activate = getattr(session, "activate_session", None)
            if callable(activate):
                try:
                    maybe = activate(command.session_id, replay_messages_limit=0)
                except TypeError:
                    maybe = activate(command.session_id)
                if inspect.isawaitable(maybe):
                    await maybe

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
                try:
                    # 会话激活默认不重放历史正文，避免把全量 messages 回灌到上下文。
                    maybe = activate(session_id, replay_messages_limit=0)
                except TypeError:
                    maybe = activate(session_id)
                if inspect.isawaitable(maybe):
                    await maybe
            self._sessions[session_id] = session
            self._owned_sessions.add(session_id)
            registry_ts = self._session_registry.get_updated_at(self._owner_id, self._source, session_id)
            self._last_activity[session_id] = registry_ts or datetime.now()
            self._session_registry.upsert_session(self._owner_id, self._source, session_id)
            return session
