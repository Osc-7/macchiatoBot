"""
CorePool — 进程加载器 + 进程表（PCB 池）。

类比操作系统的进程控制块（PCB）池：
- acquire(): 懒加载或复用 AgentCore（带 per-session 锁防重复创建）
- touch():   每次请求完成后刷新 last_active_ts，维持 TTL
- evict():   kill() + summarizer + close()，彻底回收资源
- scan_expired(): 返回超过 TTL 的 session_id 列表，供 KernelScheduler 调用

每个 CoreEntry 持有：
  agent            — AgentCore 实例
  profile          — CoreProfile（权限 + TTL 配置）
  last_active_ts   — 最近活跃时间（monotonic），用于 TTL 判断
  session_start_ts — session 创建时间（monotonic）
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from agent_core.config import Config
    from agent_core.agent.agent import ScheduleAgent
    from agent_core.tools import BaseTool
    from agent_core.kernel_interface import CoreProfile
    from .core_logger import CoreLifecycleLogger

logger = logging.getLogger(__name__)


@dataclass
class CoreEntry:
    """进程控制块（PCB）— 一个 AgentCore 实例的完整元数据。"""

    agent: "ScheduleAgent"
    profile: "CoreProfile"
    last_active_ts: float = field(default_factory=time.monotonic)
    session_start_ts: float = field(default_factory=time.monotonic)
    logger: Optional["CoreLifecycleLogger"] = None

    def is_expired(self) -> bool:
        """根据 profile.session_expired_seconds 判断是否超时。"""
        return (time.monotonic() - self.last_active_ts) > self.profile.session_expired_seconds

    def touch(self) -> None:
        """刷新最近活跃时间。"""
        self.last_active_ts = time.monotonic()


class CorePool:
    """
    AgentCore（ScheduleAgent）实例池。

    - 按 session_id 隔离
    - 懒加载：首次 acquire 时创建，后续复用
    - 每次请求完成后调用 touch() 刷新 TTL
    - scan_expired() 返回超时 session，由 KernelScheduler TTL 循环驱动 evict
    - 带 per-session asyncio.Lock 防止并发 acquire 时重复创建

    Usage::

        pool = CorePool(config=config, tools_factory=lambda: get_tools(config))
        agent = await pool.acquire("sess-001")
        # ... 使用 agent ...
        pool.touch("sess-001")       # 刷新活跃时间
        await pool.evict("sess-001") # 主动回收
    """

    def __init__(
        self,
        config: Optional["Config"] = None,
        tools_factory: Optional[Callable[[], List["BaseTool"]]] = None,
        max_sessions: int = 100,
        kernel: Optional[Any] = None,
        summarizer: Optional[Any] = None,
        session_logger: Optional[Any] = None,
    ) -> None:
        from agent_core.config import get_config

        self._config = config or get_config()
        self._tools_factory = tools_factory  # 已弃用，优先使用 system.tools.build_tool_registry
        self._max_sessions = max_sessions
        self._kernel = kernel       # AgentKernel 实例，用于 kill()
        self._summarizer = summarizer  # SessionSummarizer 实例，用于摘要持久化
        self._session_logger = session_logger  # 旧版 SessionLogger（将逐步废弃）
        # session_id → CoreEntry
        self._pool: Dict[str, CoreEntry] = {}
        # per-session 锁，防止并发创建
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def acquire(
        self,
        session_id: str,
        *,
        source: str = "cli",
        user_id: str = "root",
        create_if_missing: bool = True,
        profile: Optional["CoreProfile"] = None,
    ) -> "ScheduleAgent":
        """
        获取或创建指定 session 的 AgentCore。

        对同一 session_id 的并发 acquire 是安全的：
        内部使用 per-session Lock 保证只创建一次。
        返回 ScheduleAgent 实例（不含 CoreEntry，调用方不需要感知 PCB 细节）。
        """
        if session_id in self._pool and profile is None:
            return self._pool[session_id].agent

        lock = await self._get_lock(session_id)
        async with lock:
            if session_id in self._pool:
                entry = self._pool[session_id]
                if profile is not None:
                    await self._hot_update_profile(
                        entry=entry,
                        source=source,
                        user_id=user_id,
                        profile=profile,
                    )
                return entry.agent

            if not create_if_missing:
                raise KeyError(f"CorePool: session not found: {session_id}")

            agent, entry_profile, core_logger = await self._load(
                session_id, source=source, user_id=user_id, profile=profile
            )
            self._pool[session_id] = CoreEntry(
                agent=agent,
                profile=entry_profile,
                logger=core_logger,
            )
            logger.debug("CorePool: loaded session %s (pool_size=%d)", session_id, len(self._pool))
            return agent

    def touch(self, session_id: str) -> None:
        """刷新指定 session 的 last_active_ts，维持 TTL 倒计时。"""
        entry = self._pool.get(session_id)
        if entry is not None:
            entry.touch()

    def get_entry(self, session_id: str) -> Optional[CoreEntry]:
        """返回指定 session 的 CoreEntry（含 profile 和时间戳）。"""
        return self._pool.get(session_id)

    def scan_expired(self) -> List[str]:
        """
        返回所有已超过 TTL 的 session_id 列表。

        由 KernelScheduler 的 _ttl_loop() 定期调用，触发 evict 流程。
        """
        return [sid for sid, entry in self._pool.items() if entry.is_expired()]

    async def evict(self, session_id: str) -> None:
        """
        终结并移除指定 session 的 AgentCore。

        完整 Kill 流程（KNL-003）：
        1. AgentKernel.kill(agent)   → 收集 CoreStatsAction（token 用量等）
        2. SessionSummarizer         → 生成摘要写入长期记忆
        3. agent.close()             → 释放 MCP 连接等资源
        4. 清理 PCB（_pool + _locks）

        若未注入 kernel/summarizer，退化为旧版 finalize_session() + close()。
        """
        entry = self._pool.pop(session_id, None)
        if entry is None:
            return
        agent = entry.agent

        # ── Step 1: kill — 收集 CoreStats ──────────────────────────────────
        core_stats = None
        if self._kernel is not None:
            try:
                core_stats = await self._kernel.kill(agent)
            except Exception as exc:
                logger.warning("CorePool: kernel.kill failed (session=%s): %s", session_id, exc)
        else:
            # 向后兼容：无 kernel 时走旧的 finalize_session
            try:
                finalize = getattr(agent, "finalize_session", None)
                if callable(finalize):
                    result = finalize()
                    if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                        await result
            except Exception as exc:
                logger.warning("CorePool: finalize_session failed (session=%s): %s", session_id, exc)

        # ── Step 2: summarize — 写入长期记忆 ───────────────────────────────
        # cron 会话不持久化摘要，避免创建 data/memory/cron:xxx 等目录
        if core_stats is not None and self._summarizer is not None:
            try:
                long_term_memory = None
                if not (session_id or "").startswith("cron:"):
                    long_term_memory = getattr(agent, "_long_term_memory", None)
                messages = None
                ctx = getattr(agent, "_context", None)
                if ctx is not None:
                    get_msgs = getattr(ctx, "get_messages", None)
                    if callable(get_msgs):
                        messages = get_msgs()
                owner_id = getattr(agent, "_user_id", None)
                await self._summarizer.summarize_and_persist(
                    stats=core_stats,
                    long_term_memory=long_term_memory,
                    messages=messages,
                    owner_id=owner_id,
                )
            except Exception as exc:
                logger.warning("CorePool: summarizer failed (session=%s): %s", session_id, exc)

        # ── Step 3: close — 释放资源 ───────────────────────────────────────
        try:
            close = getattr(agent, "close", None)
            if callable(close):
                result = close()
                if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                    await result
        except RuntimeError as exc:
            # anyio/mcp 在异步生成器关闭时可能抛出：
            # RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
            # 这在 Core 已完成 evict 的情况下属于已知的无害噪音，这里与 automation_daemon 中的处理保持一致，
            # 降级为 DEBUG 级别并视为正常关闭，避免误导性 WARNING。
            msg = str(exc)
            if "cancel scope" in msg:
                logger.debug(
                    "CorePool: close teardown (ignored cancel scope error for session=%s): %s",
                    session_id,
                    exc,
                )
            else:
                logger.warning("CorePool: close failed (session=%s): %s", session_id, exc)
        except Exception as exc:
            logger.warning("CorePool: close failed (session=%s): %s", session_id, exc)

        # ── Step 4: 清理 PCB ───────────────────────────────────────────────
        async with self._global_lock:
            self._locks.pop(session_id, None)

        # Core 生命周期日志：记录 core_end 并关闭文件
        logger_obj = getattr(entry, "logger", None)
        if logger_obj is not None:
            try:
                logger_obj.on_core_end(stats=core_stats)
            except Exception:
                pass

        logger.debug("CorePool: evicted session %s", session_id)

    async def evict_all(self) -> None:
        """关闭所有 session，释放全部资源。"""
        session_ids = list(self._pool.keys())
        for sid in session_ids:
            await self.evict(sid)

    def list_sessions(self) -> List[str]:
        """返回当前活跃的 session_id 列表。"""
        return list(self._pool.keys())

    def has_session(self, session_id: str) -> bool:
        """判断 session 是否已加载到内存中。"""
        return session_id in self._pool

    async def _load(
        self,
        session_id: str,
        *,
        source: str = "cli",
        user_id: str = "root",
        profile: Optional["CoreProfile"] = None,
    ) -> tuple["ScheduleAgent", "CoreProfile", Optional["CoreLifecycleLogger"]]:
        """
        Loader 职责：从 DB 加载记忆、创建并初始化 AgentCore。

        返回 (agent, profile) 元组，profile 优先使用传入值，
        否则根据 source 生成默认 CoreProfile。
        """
        from agent_core.agent.agent import ScheduleAgent
        from agent_core.kernel_interface import CoreProfile as _CoreProfile
        from .core_logger import CoreLifecycleLogger

        if profile is None:
            if source in ("cli", "feishu"):
                profile = _CoreProfile.full_from_config(
                    self._config,
                    frontend_id=source,
                    dialog_window_id=user_id,
                )
            else:
                profile = _CoreProfile.default_full(
                    frontend_id=source,
                    dialog_window_id=user_id,
                    max_context_tokens=getattr(self._config.agent, "max_context_tokens", 80_000),
                    session_expired_seconds=getattr(self._config.agent, "session_expired_seconds", 1_800),
                )

        # 优先使用 system.tools.build_tool_registry，与 Kernel/MCP 工具装配一致
        from system.tools import build_tool_registry

        reg = build_tool_registry(
            profile=profile,
            config=self._config,
            memory_owner_id=user_id,
        )
        tools = list(reg.list_tools()[1].values())
        if not tools and self._tools_factory:
            tools = self._tools_factory()
        agent = ScheduleAgent(
            config=self._config,
            tools=tools,
            max_iterations=self._config.agent.max_iterations,
            timezone=self._config.time.timezone,
            user_id=user_id,
            source=source,
            session_logger=None,  # 关闭旧版会话日志，改用 Kernel 级 CoreLifecycleLogger
        )

        await agent.__aenter__()

        # 为该 Core 创建独立生命周期日志（按 source/user_id 归档）
        core_logger: Optional[CoreLifecycleLogger]
        try:
            log_cfg = getattr(self._config, "logging", None)
            log_dir = getattr(log_cfg, "session_log_dir", "./logs/sessions") if log_cfg else "./logs/sessions"
            core_logger = CoreLifecycleLogger(
                base_dir=log_dir,
                source=source,
                user_id=user_id,
                session_id=session_id,
            )
            core_logger.on_core_start(profile=profile)
        except Exception:
            core_logger = None

        # 将 CoreProfile 注入 agent，供 InternalLoader 过滤工具列表
        # 和 AgentKernel 进行内核态权限校验
        agent._core_profile = profile

        activate = getattr(agent, "activate_session", None)
        if callable(activate):
            result = activate(session_id)
            if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                await result

        return agent, profile, core_logger

    async def _hot_update_profile(
        self,
        *,
        entry: CoreEntry,
        source: str,
        user_id: str,
        profile: "CoreProfile",
    ) -> None:
        """在复用 session 时热更新 profile，并按新权限重装工具集。"""
        current = entry.profile
        if current == profile:
            return
        from system.tools import build_tool_registry

        reg = build_tool_registry(
            profile=profile,
            config=self._config,
            memory_owner_id=user_id,
        )
        entry.agent._tool_registry = reg
        entry.agent._source = source
        entry.agent._user_id = user_id
        entry.agent._core_profile = profile
        entry.profile = profile
        entry.touch()
        logger.info(
            "CorePool: hot-updated profile for session %s (mode=%s)",
            getattr(entry.agent, "_session_id", "unknown"),
            getattr(profile, "mode", "unknown"),
        )

    async def _get_lock(self, session_id: str) -> asyncio.Lock:
        """获取或创建指定 session 的锁（线程安全）。"""
        async with self._global_lock:
            if session_id not in self._locks:
                self._locks[session_id] = asyncio.Lock()
            return self._locks[session_id]
