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
import inspect
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from agent_core.config import Config
    from agent_core.agent.agent import AgentCore
    from agent_core.tools import BaseTool
    from agent_core.kernel_interface import CoreProfile
    from .core_logger import CoreLifecycleLogger
    from .subagent_registry import SubagentRegistry

logger = logging.getLogger(__name__)


@dataclass
class CoreEntry:
    """进程控制块（PCB）— 一个 AgentCore 实例的完整元数据。"""

    agent: "AgentCore"
    profile: "CoreProfile"
    last_active_ts: float = field(default_factory=time.monotonic)
    session_start_ts: float = field(default_factory=time.monotonic)
    logger: Optional["CoreLifecycleLogger"] = None

    def is_expired(self) -> bool:
        """根据 profile.session_expired_seconds 判断是否超时。"""
        return (
            time.monotonic() - self.last_active_ts
        ) > self.profile.session_expired_seconds

    def touch(self) -> None:
        """刷新最近活跃时间。"""
        self.last_active_ts = time.monotonic()


class CorePool:
    """
    AgentCore 实例池。

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
        subagent_registry: Optional["SubagentRegistry"] = None,
    ) -> None:
        from agent_core.config import get_config

        self._config = config or get_config()
        self._tools_factory = (
            tools_factory  # 已弃用，优先使用 system.tools.build_tool_registry
        )
        self._max_sessions = max_sessions
        self._kernel = kernel  # AgentKernel 实例，用于 kill()
        self._summarizer = summarizer  # SessionSummarizer 实例，用于摘要持久化
        self._session_logger = session_logger  # 旧版 SessionLogger（将逐步废弃）
        self._subagent_registry = subagent_registry  # SubagentRegistry，用于 subagent 工具装配
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
    ) -> "AgentCore":
        """
        获取或创建指定 session 的 AgentCore。

        对同一 session_id 的并发 acquire 是安全的：
        内部使用 per-session Lock 保证只创建一次。
        返回 AgentCore 实例（不含 CoreEntry，调用方不需要感知 PCB 细节）。
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

            # pool 容量保护：防止无限增长导致 OOM
            if len(self._pool) >= self._max_sessions:
                raise RuntimeError(
                    f"CorePool: max_sessions ({self._max_sessions}) reached; "
                    "cannot create new session"
                )

            agent, entry_profile, core_logger = await self._load(
                session_id, source=source, user_id=user_id, profile=profile
            )
            # 若从检查点恢复，用 TTL 偏移量将 last_active_ts 往回拨，
            # 使 CoreEntry.is_expired() 以"剩余 TTL"而非"满 TTL"触发。
            ttl_offset: float = getattr(agent, "_checkpoint_ttl_offset", 0.0)
            entry = CoreEntry(
                agent=agent,
                profile=entry_profile,
                logger=core_logger,
            )
            if ttl_offset > 0:
                entry.last_active_ts = time.monotonic() - ttl_offset
            self._pool[session_id] = entry
            logger.debug(
                "CorePool: loaded session %s (pool_size=%d)",
                session_id,
                len(self._pool),
            )
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

    async def evict(self, session_id: str, *, shutdown: bool = False) -> None:
        """
        终结并移除指定 session 的 AgentCore。

        完整 Kill 流程（KNL-003）：
        1. AgentKernel.kill(agent)   → 收集 CoreStatsAction（token 用量等）
        2. SessionSummarizer         → 生成摘要写入长期记忆（shutdown=True 时跳过）
        3. agent.close()             → 释放 MCP 连接等资源
        4. 清理 PCB（_pool + _locks）

        shutdown=True 时表示 kernel 正在关闭（session 只是暂停，不是真正结束）：
        - 跳过 SessionSummarizer（避免把暂停误认为 session 结束写入长期记忆）
        - 不 mark_expired（保留 checkpoint 供下次 kernel 启动恢复）

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
                logger.warning(
                    "CorePool: kernel.kill failed (session=%s): %s", session_id, exc
                )
        else:
            # 向后兼容：无 kernel 时走旧的 finalize_session
            try:
                finalize = getattr(agent, "finalize_session", None)
                if callable(finalize):
                    result = finalize()
                    if inspect.isawaitable(result):
                        await result
            except Exception as exc:
                logger.warning(
                    "CorePool: finalize_session failed (session=%s): %s",
                    session_id,
                    exc,
                )

        # ── Step 2: summarize — 写入长期记忆 ───────────────────────────────
        # background 模式（包含历史上的 cron/heartbeat）不持久化摘要到长期记忆，
        # 避免为每个后台任务创建独立 data/memory/ 前缀目录；旧版仍兼容 session_id 以
        # \"cron:\" 开头的会话不写入长期记忆。
        # shutdown=True 时跳过：session 只是暂停，checkpoint 会保留完整上下文供恢复，
        # 此时写摘要属于把暂停误认为 session 结束。
        if not shutdown and core_stats is not None and self._summarizer is not None:
            try:
                long_term_memory = None
                profile_mode = getattr(getattr(entry, "profile", None), "mode", None)
                if profile_mode != "background" and not (session_id or "").startswith(
                    "cron:"
                ):
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
                logger.warning(
                    "CorePool: summarizer failed (session=%s): %s", session_id, exc
                )

        # ── Step 3: close — 释放资源 ───────────────────────────────────────
        # shutdown=False（TTL 过期 / 主动关闭单个 session）时，标记 checkpoint 为已过期，
        # 由下次 restore_from_checkpoints() 扫描时见到 expired=True 统一清理。
        # shutdown=True（kernel 关闭）时不标记过期，保留 checkpoint 供下次恢复。
        ckpt_mgr = getattr(agent, "_checkpoint_manager", None)
        if ckpt_mgr is not None and not shutdown:
            try:
                ckpt_mgr.mark_expired()
            except Exception as exc:
                logger.debug(
                    "CorePool: checkpoint mark_expired failed (session=%s): %s", session_id, exc
                )

        try:
            close = getattr(agent, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
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
                logger.warning(
                    "CorePool: close failed (session=%s): %s", session_id, exc
                )
        except Exception as exc:
            logger.warning("CorePool: close failed (session=%s): %s", session_id, exc)

        # ── Step 4: 清理 PCB ───────────────────────────────────────────────
        # 只有在 pool 中没有该 session 的新 entry 时才删锁，防止删掉并发重建的新 session 的锁
        async with self._global_lock:
            if session_id not in self._pool:
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
        """关闭所有 session，释放全部资源。

        当前仅在 KernelScheduler.stop() 中使用，语义为 kernel 正在关闭：
        - session 视为暂停：不触发 SessionSummarizer，不标记 checkpoint 过期；
        - 仅做 kill/close + 清理 PCB，等待下次 kernel 启动根据 checkpoint 恢复。
        """
        session_ids = list(self._pool.keys())
        for sid in session_ids:
            await self.evict(sid, shutdown=True)

    def list_sessions(self) -> List[str]:
        """返回当前活跃的 session_id 列表。"""
        return list(self._pool.keys())

    def has_session(self, session_id: str) -> bool:
        """判断 session 是否已加载到内存中。"""
        return session_id in self._pool

    async def restore_from_checkpoints(self) -> int:
        """
        Kernel 启动时重建进程表（类比 OS 从持久化状态恢复进程）。

        扫描 memory_base_dir/*/*/checkpoint.json，按以下规则处理每个 checkpoint：

        1. expired=True  → 该 session 已被正常 evict，物理删除文件并跳过
        2. elapsed = kernel_last_shutdown_at - last_active_at
           elapsed >= session_ttl → 超时，标记 expired=True 并跳过
           elapsed <  session_ttl → 恢复为活跃 Core：
               - 通过 acquire() → _load() 重建 AgentCore 并调用 restore_from_checkpoint
               - CoreEntry.last_active_ts = monotonic() - elapsed（TTL 从剩余时间继续计时）

        恢复后的 Core 完全交由现有 TTL 监控路径（scan_expired → evict）管理。

        Returns:
            成功恢复的 session 数量
        """
        from agent_core.agent.checkpoint import CoreCheckpointManager
        from agent_core.agent.memory_paths import get_kernel_shutdown_at_path

        mem_cfg = self._config.memory
        base_dir = Path((mem_cfg.memory_base_dir or "./data/memory").strip())

        # 读取 kernel 关闭时间戳；无则无法判断 elapsed，跳过所有恢复
        shutdown_path = Path(get_kernel_shutdown_at_path(mem_cfg))
        if not shutdown_path.exists():
            logger.debug(
                "CorePool.restore_from_checkpoints: no shutdown timestamp, skipping"
            )
            return 0
        try:
            shutdown_at = float(shutdown_path.read_text(encoding="utf-8").strip())
        except Exception as exc:
            logger.warning(
                "CorePool.restore_from_checkpoints: failed to read shutdown_at: %s", exc
            )
            return 0

        checkpoint_files = list(base_dir.glob("*/*/checkpoint.json"))
        if not checkpoint_files:
            return 0

        restored = 0
        for ckpt_file in checkpoint_files:
            mgr = CoreCheckpointManager(str(ckpt_file))
            ckpt = mgr.read()
            if ckpt is None:
                continue

            session_id = ckpt.session_id

            # ① 已被正常 evict：清理文件并跳过
            if ckpt.expired:
                try:
                    ckpt_file.unlink()
                except Exception:
                    pass
                logger.debug(
                    "CorePool.restore_from_checkpoints: cleaned up evicted checkpoint "
                    "session=%s (%s)",
                    session_id, ckpt_file,
                )
                continue

            # cron/background session 不恢复
            if not session_id or session_id.startswith("cron:"):
                continue

            # 已在 pool 中（不应发生，但防御性跳过）
            if session_id in self._pool:
                continue

            # ② 判断是否超时：elapsed = shutdown_at - last_active_at
            # max(0.0, ...) 防御 NTP 时钟回拨导致 elapsed 为负（视为无时间流逝，session 不过期）
            elapsed = max(0.0, shutdown_at - ckpt.last_active_at)
            session_ttl = ckpt.remaining_ttl_seconds or float(
                getattr(self._config.agent, "session_expired_seconds", 1800)
            )
            if elapsed >= session_ttl:
                # 超时：标记 expired=True，供下次启动清理
                mgr.mark_expired()
                logger.debug(
                    "CorePool.restore_from_checkpoints: checkpoint expired session=%s "
                    "(elapsed=%.0fs >= ttl=%.0fs)",
                    session_id, elapsed, session_ttl,
                )
                continue

            # ③ 未过期：通过 acquire() 重建 Core（内部调用 _load() + restore_from_checkpoint）
            try:
                await self.acquire(
                    session_id,
                    source=ckpt.source,
                    user_id=ckpt.owner_id,
                )
                restored += 1
                logger.info(
                    "CorePool.restore_from_checkpoints: restored session=%s "
                    "source=%s user=%s (elapsed=%.0fs, remaining=%.0fs)",
                    session_id, ckpt.source, ckpt.owner_id,
                    elapsed, session_ttl - elapsed,
                )
            except Exception as exc:
                logger.warning(
                    "CorePool.restore_from_checkpoints: failed to restore session=%s: %s",
                    session_id, exc,
                )

        if restored:
            logger.info(
                "CorePool.restore_from_checkpoints: restored %d session(s) into pool",
                restored,
            )
        return restored

    async def _load(
        self,
        session_id: str,
        *,
        source: str = "cli",
        user_id: str = "root",
        profile: Optional["CoreProfile"] = None,
    ) -> tuple["AgentCore", "CoreProfile", Optional["CoreLifecycleLogger"]]:
        """
        Loader 职责：从 DB 加载记忆、创建并初始化 AgentCore。

        返回 (agent, profile) 元组，profile 优先使用传入值，
        否则根据 source 生成默认 CoreProfile。

        检查点恢复（TTL 暂停语义）：
        若 data/memory/{source}/{user_id}/checkpoint.json 存在且 remaining_ttl_seconds > 0，
        则通过 restore_from_checkpoint 直接恢复 WorkingMemory 状态，
        跳过 activate_session 的 ChatHistoryDB 全量重放。
        """
        from agent_core.agent.agent import AgentCore
        from agent_core.agent.checkpoint import CoreCheckpointManager
        from agent_core.agent.memory_paths import resolve_memory_owner_paths
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
                    max_context_tokens=getattr(
                        self._config.agent, "max_context_tokens", 80_000
                    ),
                    session_expired_seconds=getattr(
                        self._config.agent, "session_expired_seconds", 1_800
                    ),
                )

        # 优先使用 system.tools.build_tool_registry，与 Kernel/MCP 工具装配一致
        from system.tools import build_tool_registry

        reg = build_tool_registry(
            profile=profile,
            config=self._config,
            memory_owner_id=user_id,
            subagent_registry=self._subagent_registry,
            core_pool=self,
        )
        tools = list(reg.list_tools()[1].values())
        # search_tools / call_tool 需绑定 AgentCore 自身的 ToolWorkingSetManager；
        # build_tool_registry() 中创建的实例绑定的是外部 ToolWorkingSetManager，
        # 若直接传入 AgentCore 会触发 has("search_tools") 守卫、跳过内部正确版本的创建，
        # 导致 search_tools 更新的工作集被 InternalLoader 忽略。
        # 解决方案：过滤掉这两个工具，AgentCore.__init__ 会用正确的 working_set 重新注册它们。
        tools = [t for t in tools if t.name not in {"search_tools", "call_tool"}]
        if not tools and self._tools_factory:
            tools = self._tools_factory()
        # 是否为该 Core 启用本地记忆库：默认跟随配置，
        # 但允许 CoreProfile（如 cron/heartbeat）按 Core 粒度关闭，避免创建一次性 owner 目录。
        memory_enabled = getattr(profile, "memory_enabled", True)

        agent = AgentCore(
            config=self._config,
            tools=tools,
            max_iterations=self._config.agent.max_iterations,
            timezone=self._config.time.timezone,
            user_id=user_id,
            source=source,
            session_logger=None,  # 关闭旧版会话日志，改用 Kernel 级 CoreLifecycleLogger
            memory_enabled=memory_enabled,
        )

        await agent.__aenter__()

        # __aenter__ 之后若任何初始化步骤抛出异常，必须保证 __aexit__ 被调用，
        # 否则 MCP 连接、文件句柄等资源将永久泄漏。
        try:
            # 为该 Core 创建独立生命周期日志（按 source/user_id 归档）
            core_logger: Optional[CoreLifecycleLogger]
            try:
                log_cfg = getattr(self._config, "logging", None)
                log_dir = (
                    getattr(log_cfg, "session_log_dir", "./logs/sessions")
                    if log_cfg
                    else "./logs/sessions"
                )
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

            # ── 检查点恢复 vs 冷启动 ──────────────────────────────────────────
            # 过期判断：elapsed = kernel_last_shutdown_at - checkpoint.last_active_at；
            # 仅当 kernel 曾写入关闭时间戳且 elapsed < TTL 时恢复，否则冷启动或标记过期并删 checkpoint。
            profile_mode = getattr(profile, "mode", None)
            use_checkpoint = memory_enabled and profile_mode != "background" and not (
                session_id or ""
            ).startswith("cron:")

            restored_from_checkpoint = False
            initial_ttl_offset: float = 0.0  # 恢复时 entry.last_active_ts = monotonic() - elapsed

            if use_checkpoint:
                try:
                    from agent_core.agent.memory_paths import get_kernel_shutdown_at_path

                    mem_cfg = self._config.memory
                    mem_paths = resolve_memory_owner_paths(
                        mem_cfg, user_id, config=self._config, source=source
                    )
                    ckpt_mgr = CoreCheckpointManager(mem_paths["checkpoint_path"])
                    checkpoint = ckpt_mgr.read()

                    # expired=True：该 session 已被正常 evict，清理文件并走冷启动
                    if checkpoint is not None and checkpoint.expired:
                        ckpt_mgr.delete()
                        checkpoint = None
                        logger.debug(
                            "CorePool._load: cleaned up evicted checkpoint (session=%s)", session_id
                        )

                    if checkpoint is not None and checkpoint.session_id == session_id:
                        shutdown_path = get_kernel_shutdown_at_path(mem_cfg)
                        shutdown_at: Optional[float] = None
                        if Path(shutdown_path).exists():
                            try:
                                shutdown_at = float(
                                    Path(shutdown_path).read_text(encoding="utf-8").strip()
                                )
                            except Exception:
                                pass

                        if shutdown_at is not None:
                            session_ttl = float(
                                getattr(profile, "session_expired_seconds", 1800)
                            )
                            # max(0.0, ...) 防御 NTP 时钟回拨（elapsed 为负时视为 0，保留 session）
                            elapsed = max(0.0, shutdown_at - checkpoint.last_active_at)
                            if elapsed >= session_ttl:
                                # 超时：标记过期，冷启动
                                ckpt_mgr.mark_expired()
                                logger.debug(
                                    "CorePool._load: checkpoint expired (session=%s "
                                    "elapsed=%.0fs >= ttl=%.0fs)",
                                    session_id, elapsed, session_ttl,
                                )
                            else:
                                restore_fn = getattr(agent, "restore_from_checkpoint", None)
                                if callable(restore_fn):
                                    restore_fn(checkpoint)
                                    restored_from_checkpoint = True
                                    initial_ttl_offset = elapsed
                                    logger.info(
                                        "CorePool._load: restored checkpoint for session=%s "
                                        "(elapsed=%.0fs, remaining=%.0fs)",
                                        session_id, elapsed, session_ttl - elapsed,
                                    )
                except Exception as exc:
                    logger.warning(
                        "CorePool._load: checkpoint restore failed (session=%s), "
                        "falling back to cold start: %s",
                        session_id,
                        exc,
                    )

            if not restored_from_checkpoint:
                activate = getattr(agent, "activate_session", None)
                if callable(activate):
                    result = activate(session_id)
                    if inspect.isawaitable(result):
                        await result

            # 将 TTL 偏移量附到返回值，供 CorePool.acquire() 修正 CoreEntry 时间戳
            agent._checkpoint_ttl_offset = initial_ttl_offset  # type: ignore[attr-defined]

            return agent, profile, core_logger

        except BaseException:
            # 初始化失败：确保释放 __aenter__ 已获取的资源（MCP 连接等）
            try:
                await agent.__aexit__(None, None, None)
            except Exception as _exit_exc:
                logger.warning(
                    "CorePool._load: __aexit__ failed during error cleanup (session=%s): %s",
                    session_id, _exit_exc,
                )
            raise

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
            subagent_registry=self._subagent_registry,
            core_pool=self,
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
