"""
CorePool — 进程加载器 + 进程表。

类比操作系统的进程控制块（PCB）池：
- acquire(): 懒加载或复用 AgentCore（带 per-session 锁防重复创建）
- release(): 持久 session 保持活跃，临时 session 立即 evict
- evict(): finalize_session() + close()，彻底回收资源

CorePool 是 Loader 职责的实现处：
  创建（_load）和回收（evict）是同一资源的两面，放在同一个类里
  保证 AgentCore 生命周期清晰，不会出现悬空 Core 或重复创建。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from agent.config import Config
    from agent.core.agent.agent import ScheduleAgent
    from agent.core.tools import BaseTool

logger = logging.getLogger(__name__)


class CorePool:
    """
    AgentCore（ScheduleAgent）实例池。

    - 按 session_id 隔离
    - 懒加载：首次 acquire 时创建，后续复用
    - 支持 ephemeral（用完即销毁）和 persistent（保持活跃）两种策略
    - 带 per-session asyncio.Lock 防止并发 acquire 时重复创建

    Usage::

        pool = CorePool(config=config, tools_factory=lambda: get_tools(config))
        agent = await pool.acquire("sess-001")
        # ... 使用 agent ...
        # persistent: 保持活跃，下次 acquire 直接复用
        # ephemeral: 使用完后主动 evict
        await pool.evict("sess-001")
    """

    def __init__(
        self,
        config: Optional["Config"] = None,
        tools_factory: Optional[Callable[[], List["BaseTool"]]] = None,
        max_sessions: int = 100,
    ) -> None:
        from agent.config import get_config

        self._config = config or get_config()
        self._tools_factory = tools_factory
        self._max_sessions = max_sessions
        # session_id → ScheduleAgent
        self._pool: Dict[str, "ScheduleAgent"] = {}
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
    ) -> "ScheduleAgent":
        """
        获取或创建指定 session 的 AgentCore。

        对同一 session_id 的并发 acquire 是安全的：
        内部使用 per-session Lock 保证只创建一次。
        """
        if session_id in self._pool:
            return self._pool[session_id]

        lock = await self._get_lock(session_id)
        async with lock:
            # 双重检查（在获取锁之前可能另一个协程已经创建了）
            if session_id in self._pool:
                return self._pool[session_id]

            if not create_if_missing:
                raise KeyError(f"CorePool: session not found: {session_id}")

            agent = await self._load(session_id, source=source, user_id=user_id)
            self._pool[session_id] = agent
            logger.debug("CorePool: loaded session %s (pool_size=%d)", session_id, len(self._pool))
            return agent

    async def evict(self, session_id: str) -> None:
        """
        终结并移除指定 session 的 AgentCore。

        执行 finalize_session()（写长期记忆摘要）后 close() 释放资源。
        对应 OS 中的进程终止（exit syscall）。
        """
        agent = self._pool.pop(session_id, None)
        if agent is None:
            return

        try:
            finalize = getattr(agent, "finalize_session", None)
            if callable(finalize):
                result = finalize()
                if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                    await result
        except Exception as exc:
            logger.warning("CorePool: finalize_session failed (session=%s): %s", session_id, exc)

        try:
            close = getattr(agent, "close", None)
            if callable(close):
                result = close()
                if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                    await result
        except Exception as exc:
            logger.warning("CorePool: close failed (session=%s): %s", session_id, exc)

        # 清理 per-session 锁
        async with self._global_lock:
            self._locks.pop(session_id, None)

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
    ) -> "ScheduleAgent":
        """
        Loader 职责：从 DB 加载记忆、创建并初始化 AgentCore。

        对应架构图中 Loader 从 Database 加载数据并实例化 AgentCore 的步骤。
        """
        # 延迟导入，避免循环依赖
        from agent.core.agent.agent import ScheduleAgent

        tools = self._tools_factory() if self._tools_factory else []
        agent = ScheduleAgent(
            config=self._config,
            tools=tools,
            max_iterations=self._config.agent.max_iterations,
            timezone=self._config.time.timezone,
            user_id=user_id,
            source=source,
        )

        # 确保 MCP 连接（首次创建时通过 __aenter__ 建立）
        await agent.__aenter__()

        # 从持久化历史恢复会话上下文
        activate = getattr(agent, "activate_session", None)
        if callable(activate):
            result = activate(session_id)
            if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                await result

        return agent

    async def _get_lock(self, session_id: str) -> asyncio.Lock:
        """获取或创建指定 session 的锁（线程安全）。"""
        async with self._global_lock:
            if session_id not in self._locks:
                self._locks[session_id] = asyncio.Lock()
            return self._locks[session_id]
