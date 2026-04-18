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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Literal, Optional

from system.multi_agent.constants import METADATA_KEY_AGENT_MESSAGE

if TYPE_CHECKING:
    from agent_core.config import Config
    from agent_core.agent.agent import AgentCore
    from agent_core.kernel_interface import CoreProfile
    from .core_logger import CoreLifecycleLogger
    from .scheduler import KernelScheduler

logger = logging.getLogger(__name__)


@dataclass
class CoreEntry:
    """进程控制块（PCB）— 一个 AgentCore 实例的完整元数据。"""

    agent: Optional["AgentCore"]
    profile: "CoreProfile"
    created_at: float = field(default_factory=time.time)
    last_active_ts: float = field(default_factory=time.monotonic)
    session_start_ts: float = field(default_factory=time.monotonic)
    logger: Optional["CoreLifecycleLogger"] = None
    parent_session_id: Optional[str] = None
    task_description: Optional[str] = None
    bg_task: Optional[asyncio.Task[Any]] = None
    sub_status: Optional[Literal["running", "completed", "failed", "cancelled"]] = None
    sub_result: Optional[str] = None
    sub_error: Optional[str] = None
    sub_completed_at: Optional[float] = None
    # 飞书私聊：首轮用户消息写入，供 inject_turn 解析 chat_id（群聊可从 session_id 解析）
    feishu_chat_id: Optional[str] = None

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

        pool = CorePool(config=config)
        agent = await pool.acquire("sess-001")
        # ... 使用 agent ...
        pool.touch("sess-001")       # 刷新活跃时间
        await pool.evict("sess-001") # 主动回收
    """

    def __init__(
        self,
        config: Optional["Config"] = None,
        max_sessions: int = 100,
        kernel: Optional[Any] = None,
        summarizer: Optional[Any] = None,
        session_logger: Optional[Any] = None,
    ) -> None:
        from agent_core.config import get_config

        self._config = config or get_config()
        self._max_sessions = max_sessions
        self._kernel = kernel  # AgentKernel 实例，用于 kill()
        self._summarizer = summarizer  # SessionSummarizer 实例，用于摘要持久化
        self._session_logger = session_logger  # 旧版 SessionLogger（将逐步废弃）
        self._scheduler: Optional["KernelScheduler"] = None
        # session_id → CoreEntry
        self._pool: Dict[str, CoreEntry] = {}
        # 已结束待收割的子进程骸体：session_id -> stripped CoreEntry
        self._zombies: Dict[str, CoreEntry] = {}
        # sub:xxx -> time.time()：本进程内已成功 reap_zombie 后记入，用于区分「重复 reap」与「错误 id」
        self._reaped_subagent_at: Dict[str, float] = {}
        # sub:xxx：父已通过 wait_subagent（阻塞路径）拿到终态；子先于 wait 完成时 on_sub_complete 仍会 inject，迟到注入应丢弃
        self._parent_got_terminal_via_wait_tool: set[str] = set()
        # sub:xxx -> 待注入父侧的正文：先入暂存区，仅在父会话 kernel 请求「安全点」flush（类比 OS 延后信号）
        self._pending_subagent_lifecycle: Dict[str, str] = {}
        # per-session 锁，防止并发创建
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        # session_id -> 用户为该会话选择的主对话 LLM provider 名；首次 _load 创建 AgentCore 后会 apply。
        self._session_preferred_llm_provider: Dict[str, str] = {}

    def set_scheduler(self, scheduler: "KernelScheduler") -> None:
        """后绑定 KernelScheduler，供子进程完成时 inject_turn。"""
        self._scheduler = scheduler

    def get_session_preferred_llm_provider(self, session_id: str) -> Optional[str]:
        """该会话在尚未物化 Core 时预选的主模型 provider 名（与 config.llm.providers 的 key 一致）。"""
        sid = (session_id or "").strip()
        return self._session_preferred_llm_provider.get(sid)

    def set_session_preferred_llm_provider(self, session_id: str, name: str) -> None:
        """记录会话级主模型选择；首次 acquire/_load 时会 ``switch_model`` 到该 provider。"""
        from agent_core.llm.provider_resolve import resolve_llm_provider_key

        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("session_id 不能为空")
        raw = (name or "").strip()
        if not raw:
            raise ValueError("provider 名不能为空")
        key = resolve_llm_provider_key(self._config.llm, raw)
        self._session_preferred_llm_provider[sid] = key

    def clear_session_preferred_llm_provider(self, session_id: str) -> None:
        """删除会话的预选主模型（如会话已从注册表删除时由 gateway 调用）。"""
        self._session_preferred_llm_provider.pop((session_id or "").strip(), None)

    def mark_parent_got_terminal_via_wait_subagent_tool(self, sub_session_id: str) -> None:
        """父会话 wait_subagent 已成功返回终态快照后调用，用于抑制重复的 [子任务 x 完成] inject。"""
        sid = (sub_session_id or "").strip()
        if sid.startswith("sub:"):
            self._parent_got_terminal_via_wait_tool.add(sid)

    def should_suppress_lifecycle_inject_after_wait_tool(self, sub_session_id: str) -> bool:
        return (sub_session_id or "").strip() in self._parent_got_terminal_via_wait_tool

    def discard_pending_subagent_lifecycle_inject(self, sub_session_id: str) -> None:
        """丢弃暂存区中该子会话的生命周期通知（如父即将阻塞 wait_subagent，终态仅由工具返回）。"""
        self._pending_subagent_lifecycle.pop((sub_session_id or "").strip(), None)

    def flush_pending_subagent_lifecycle_for_parent(self, parent_session_id: str) -> None:
        """将指向该父会话的暂存通知注入 kernel（在父会话 inflight 归零或本轮 _run_and_route 结束时调用）。"""
        pid = (parent_session_id or "").strip()
        if not pid:
            return
        for sub_sid in list(self._pending_subagent_lifecycle.keys()):
            ent = self._pool.get(sub_sid) or self._zombies.get(sub_sid)
            if ent is None:
                self._pending_subagent_lifecycle.pop(sub_sid, None)
                continue
            if (ent.parent_session_id or "").strip() != pid:
                continue
            if self.should_suppress_lifecycle_inject_after_wait_tool(sub_sid):
                self._pending_subagent_lifecycle.pop(sub_sid, None)
                logger.info(
                    "CorePool: drop pending lifecycle (parent got terminal via wait_subagent) "
                    "session_id=%s parent_session_id=%s",
                    sub_sid,
                    pid,
                    extra={"session_id": sub_sid, "parent_session_id": pid},
                )
                continue
            if sub_sid.startswith("sub:") and self.was_subagent_reaped_in_process(sub_sid):
                self._pending_subagent_lifecycle.pop(sub_sid, None)
                logger.info(
                    "CorePool: drop pending lifecycle (child reaped) session_id=%s",
                    sub_sid,
                    extra={"session_id": sub_sid},
                )
                continue
            notification = self._pending_subagent_lifecycle.pop(sub_sid, None)
            if not notification:
                continue
            self._inject_to_parent(sub_sid, ent, notification)

    def _stage_parent_lifecycle_notification(self, sub_session_id: str, notification: str) -> None:
        """子终态且未通过 notify 唤醒 wait：写入暂存区；父会话当前无 inflight 请求时立即 flush。"""
        sid = (sub_session_id or "").strip()
        self._pending_subagent_lifecycle[sid] = notification
        entry = self._pool.get(sid) or self._zombies.get(sid)
        if entry is None:
            return
        parent_id = (entry.parent_session_id or "").strip()
        if not parent_id:
            return
        sched = self._scheduler
        if sched is not None:
            cnt_fn = getattr(sched, "session_inflight_request_count", None)
            if callable(cnt_fn):
                try:
                    n = int(cnt_fn(parent_id))
                    if n > 0:
                        logger.debug(
                            "CorePool: staged subagent lifecycle (parent has inflight kernel request) "
                            "sub_session_id=%s parent_session_id=%s",
                            sid,
                            parent_id,
                        )
                        return
                except (TypeError, ValueError):
                    pass
        self.flush_pending_subagent_lifecycle_for_parent(parent_id)

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
        existing = self._pool.get(session_id)
        if existing is not None and existing.agent is not None and profile is None:
            return existing.agent

        lock = await self._get_lock(session_id)
        async with lock:
            if session_id in self._pool and self._pool[session_id].agent is not None:
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

            # 无显式 profile 时继承占位 / zombie 上的 CoreProfile。
            # 典型场景：子任务已完成并 evict 后，父通过 send_message_to_agent 再次 inject_turn，
            # 请求不带 profile；若此处误用 default_full，子会话会以 full 工具集回复自然语言，
            # 不会调用 reply_to_message，导致父侧 P2P 阻塞至超时。
            load_profile = profile
            if load_profile is None:
                holder = self._pool.get(session_id) or self._zombies.get(session_id)
                if holder is not None and getattr(holder, "profile", None) is not None:
                    load_profile = holder.profile

            agent, entry_profile, core_logger = await self._load(
                session_id, source=source, user_id=user_id, profile=load_profile
            )
            # 若从检查点恢复，用 TTL 偏移量将 last_active_ts 往回拨，
            # 使 CoreEntry.is_expired() 以"剩余 TTL"而非"满 TTL"触发。
            ttl_offset: float = getattr(agent, "_checkpoint_ttl_offset", 0.0)
            entry = CoreEntry(
                agent=agent,
                profile=entry_profile,
                logger=core_logger,
            )
            # 占位符在 _pool、或仅存在于 _zombies（子任务已 evict 后因 inject_turn 再次加载）
            zombie_prev = self._zombies.get(session_id)
            if existing is not None:
                entry.created_at = existing.created_at
                entry.last_active_ts = existing.last_active_ts
                entry.session_start_ts = existing.session_start_ts
                entry.parent_session_id = existing.parent_session_id
                entry.task_description = existing.task_description
                entry.bg_task = existing.bg_task
                entry.sub_status = existing.sub_status
                entry.sub_result = existing.sub_result
                entry.sub_error = existing.sub_error
                entry.sub_completed_at = existing.sub_completed_at
                entry.feishu_chat_id = existing.feishu_chat_id
                if zombie_prev is not None:
                    # 不应与 register_sub 占位并存；丢弃陈旧 zombie，避免重复 PCB
                    self._zombies.pop(session_id, None)
            elif zombie_prev is not None:
                # 子 Agent 已结束并入 zombie 后，再次 acquire（如 reply_to_message 注入子会话）
                # 须继承 sub_status / sub_result，否则 get_subagent_status 会误报 running
                self._zombies.pop(session_id, None)
                entry.created_at = zombie_prev.created_at
                entry.last_active_ts = zombie_prev.last_active_ts
                entry.session_start_ts = zombie_prev.session_start_ts
                entry.parent_session_id = zombie_prev.parent_session_id
                entry.task_description = zombie_prev.task_description
                entry.bg_task = zombie_prev.bg_task
                entry.sub_status = zombie_prev.sub_status
                entry.sub_result = zombie_prev.sub_result
                entry.sub_error = zombie_prev.sub_error
                entry.sub_completed_at = zombie_prev.sub_completed_at
                entry.feishu_chat_id = zombie_prev.feishu_chat_id
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
        """返回指定 session 的 CoreEntry（优先活跃表，其次 zombie 表）。"""
        return self._pool.get(session_id) or self._zombies.get(session_id)

    def get_live_entry(self, session_id: str) -> Optional[CoreEntry]:
        """仅返回活跃进程表中的条目。"""
        return self._pool.get(session_id)

    def list_entries(self, *, include_zombies: bool = False) -> List[tuple[str, CoreEntry]]:
        """列出进程表条目。"""
        items = list(self._pool.items())
        if include_zombies:
            items.extend(self._zombies.items())
        return items

    def zombie_count(self) -> int:
        return len(self._zombies)

    def is_zombie(self, session_id: str) -> bool:
        return session_id in self._zombies

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
        # 不在此丢弃 _pending_subagent_lifecycle：子任务线程里 on_sub_complete 可能已将通知
        # 暂存（父会话仍有 inflight），随后本方法在 finally 中 evict 子会话。若此处 pop 暂存，
        # 父侧永远不会再收到 inject_turn。显式丢弃仍由 wait_subagent / reap_zombie 负责。
        agent = entry.agent
        is_subagent = bool(entry.parent_session_id) or session_id.startswith("sub:")

        # 子 Agent 已终态时，尽快写入 zombie 表。否则后续 kill / summarize / close 多为长时间
        # await，此窗口内 get_entry 既不在 _pool 也未登记 _zombies，父会话会收到完成注入但
        # get_subagent_status / reap_subagent 恒为 SUBAGENT_NOT_FOUND（竞态）。
        if (
            not shutdown
            and is_subagent
            and entry.sub_status in {"completed", "failed", "cancelled"}
        ):
            self._zombies[session_id] = self._strip_entry_for_zombie(entry)

        # ── Step 1: kill — 收集 CoreStats ──────────────────────────────────
        core_stats = None
        if agent is None:
            core_stats = None
        elif self._kernel is not None:
            try:
                core_stats = await self._kernel.kill(agent)
            except Exception as exc:
                logger.warning(
                    "CorePool: kernel.kill failed (session=%s): %s", session_id, exc
                )
        elif agent is not None:
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

        # Kernel 关闭路径：在释放资源前刷新 checkpoint，使 last_active_at 接近关闭时刻，
        # 避免恢复时用「上一轮 turn 的 wall 时间」与 shutdown_at 相减导致误判超时。
        if shutdown and agent is not None:
            flush = getattr(agent, "flush_checkpoint_for_shutdown", None)
            if callable(flush):
                try:
                    flush()
                except Exception as exc:
                    logger.warning(
                        "CorePool: flush_checkpoint_for_shutdown failed (session=%s): %s",
                        session_id,
                        exc,
                    )

        # ── Step 2: summarize — 写入长期记忆 ───────────────────────────────
        # background 模式不跑会话摘要：多为高频定时任务，避免长期记忆被刷屏。
        # sub 模式（子 Agent）为一次性任务：可交付结果在 sub_result / zombie，由父 get / reap
        # 拉取即可；不必再跑 LLM 会话摘要写入 data/memory/subagent/<id>/long_term（重复、费 token、拉长 evict）。
        # full（含带 memory_owner 的 cron 任务）使用与主会话一致的 LongTermMemory 时应正常摘要；
        # 此前误用 session_id.startswith("cron:") 一刀切，导致如 moltbook full 任务 evict 时从不写入 long_term。
        # shutdown=True 时跳过：session 只是暂停，checkpoint 会保留完整上下文供恢复，
        # 此时写摘要属于把暂停误认为 session 结束。
        _prof_mode = getattr(getattr(entry, "profile", None), "mode", None)
        if (
            agent is not None
            and not shutdown
            and core_stats is not None
            and self._summarizer is not None
            and _prof_mode != "sub"
        ):
            try:
                long_term_memory = None
                profile_mode = _prof_mode
                if profile_mode != "background":
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
        ckpt_mgr = getattr(agent, "_checkpoint_manager", None) if agent is not None else None
        if ckpt_mgr is not None and not shutdown:
            try:
                ckpt_mgr.mark_expired()
            except Exception as exc:
                logger.debug(
                    "CorePool: checkpoint mark_expired failed (session=%s): %s", session_id, exc
                )

        try:
            close = getattr(agent, "close", None) if agent is not None else None
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

        # Core 生命周期日志：仅当 session 真正结束（非 daemon 暂停）时记录 core_end
        # shutdown=True：daemon 停止，session 视为暂停，checkpoint 会保留供恢复，不写 core_end
        # shutdown=False：TTL 过期或主动关闭，session 已结束，写 core_end
        logger_obj = getattr(entry, "logger", None)
        if logger_obj is not None:
            try:
                if shutdown:
                    logger_obj.close()
                else:
                    logger_obj.on_core_end(stats=core_stats)
            except Exception:
                pass

        if not shutdown and is_subagent and entry.sub_status in {"completed", "failed", "cancelled"}:
            self._zombies[session_id] = self._strip_entry_for_zombie(entry)
            logger.info(
                "CorePool: subagent evicted session_id=%s parent_session_id=%s sub_status=%s "
                "(zombie PCB retained until parent reap_subagent)",
                session_id,
                entry.parent_session_id or "",
                entry.sub_status,
                extra={
                    "session_id": session_id,
                    "parent_session_id": entry.parent_session_id,
                    "sub_status": entry.sub_status,
                },
            )

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
        return session_id in self._pool or session_id in self._zombies

    def register_sub(
        self,
        *,
        sub_session_id: str,
        parent_session_id: str,
        task_description: str,
        profile: Optional["CoreProfile"] = None,
    ) -> CoreEntry:
        """在进程表中注册一个子进程占位条目。"""
        from agent_core.kernel_interface import CoreProfile as _CoreProfile

        subagent_id = sub_session_id[4:] if sub_session_id.startswith("sub:") else sub_session_id
        entry_profile = profile or _CoreProfile.default_sub(
            allowed_tools=None,
            frontend_id="subagent",
            dialog_window_id=subagent_id,
            tools_config=self._config.tools,
        )
        entry = CoreEntry(
            agent=None,
            profile=entry_profile,
            parent_session_id=parent_session_id,
            task_description=task_description,
            sub_status="running",
        )
        self._zombies.pop(sub_session_id, None)
        self._pool[sub_session_id] = entry
        desc = task_description or ""
        task_preview = desc[:80].replace("\n", " ")
        if len(desc) > 80:
            task_preview += "..."
        logger.info(
            "CorePool: registered sub session_id=%s parent_session_id=%s task_preview=%s",
            sub_session_id,
            parent_session_id,
            task_preview,
            extra={"session_id": sub_session_id, "parent_session_id": parent_session_id},
        )
        return entry

    def list_subs_by_parent(self, parent_session_id: str) -> List[CoreEntry]:
        return [
            entry
            for _, entry in self.list_entries(include_zombies=True)
            if entry.parent_session_id == parent_session_id
        ]

    def get_sub_info(self, sub_session_id: str) -> Optional[CoreEntry]:
        return self.get_entry(sub_session_id)

    def on_sub_complete(self, sub_session_id: str, result: str) -> None:
        entry = self._pool.get(sub_session_id) or self._zombies.get(sub_session_id)
        if entry is None:
            logger.warning("CorePool.on_sub_complete: unknown session_id=%s", sub_session_id)
            return
        if entry.sub_status == "cancelled":
            logger.info(
                "CorePool.on_sub_complete: ignoring cancelled session_id=%s",
                sub_session_id,
            )
            return
        entry.sub_status = "completed"
        entry.sub_result = result
        entry.sub_error = None
        entry.sub_completed_at = time.time()
        duration_sec = entry.sub_completed_at - entry.created_at if entry.created_at else None
        logger.info(
            "CorePool: subagent completed session_id=%s parent_session_id=%s result_len=%s duration_sec=%s",
            sub_session_id,
            entry.parent_session_id,
            len(result),
            round(duration_sec, 2) if duration_sec is not None else None,
            extra={"session_id": sub_session_id, "parent_session_id": entry.parent_session_id, "status": "completed"},
        )
        woke_waiter = False
        if self._scheduler is not None:
            notify = getattr(self._scheduler, "notify_subagent_terminal_waiter", None)
            if callable(notify):
                woke_waiter = bool(notify(sub_session_id))
        if woke_waiter:
            logger.info(
                "CorePool: skip inject_to_parent (parent wait_subagent already received terminal) "
                "session_id=%s parent_session_id=%s",
                sub_session_id,
                entry.parent_session_id,
                extra={"session_id": sub_session_id, "parent_session_id": entry.parent_session_id},
            )
            return
        task_preview = (entry.task_description or "")[:80]
        result_preview = (result or "")[:200]
        ellipsis = "..." if len(result or "") > 200 else ""
        sid = self._subagent_id(sub_session_id)
        notification = (
            f"[子任务 {sid} 完成]\n"
            f"任务：{task_preview}\n"
            f"结果预览：{result_preview}{ellipsis}\n\n"
            f"如需只读完整结果：get_subagent_status(subagent_id=\"{sid}\", include_full_result=True)。"
            f"确认不再需要子工作区文件后，调用 reap_subagent(subagent_id=\"{sid}\") 完成收割。"
        )
        self._stage_parent_lifecycle_notification(sub_session_id, notification)

    def on_sub_fail(self, sub_session_id: str, error: str) -> None:
        entry = self._pool.get(sub_session_id) or self._zombies.get(sub_session_id)
        if entry is None:
            logger.warning("CorePool.on_sub_fail: unknown session_id=%s", sub_session_id)
            return
        if entry.sub_status == "cancelled":
            logger.info(
                "CorePool.on_sub_fail: ignoring cancelled session_id=%s",
                sub_session_id,
            )
            return
        entry.sub_status = "failed"
        entry.sub_error = error
        entry.sub_completed_at = time.time()
        duration_sec = entry.sub_completed_at - entry.created_at if entry.created_at else None
        error_preview = (error or "")[:200].replace("\n", " ")
        logger.info(
            "CorePool: subagent failed session_id=%s parent_session_id=%s duration_sec=%s error_preview=%s",
            sub_session_id,
            entry.parent_session_id,
            round(duration_sec, 2) if duration_sec is not None else None,
            error_preview + ("..." if len(error or "") > 200 else ""),
            extra={"session_id": sub_session_id, "parent_session_id": entry.parent_session_id, "status": "failed"},
        )
        logger.debug("CorePool: subagent full error session_id=%s error=%s", sub_session_id, error)
        woke_waiter = False
        if self._scheduler is not None:
            notify = getattr(self._scheduler, "notify_subagent_terminal_waiter", None)
            if callable(notify):
                woke_waiter = bool(notify(sub_session_id))
        if woke_waiter:
            logger.info(
                "CorePool: skip inject_to_parent (parent wait_subagent already received terminal) "
                "session_id=%s parent_session_id=%s",
                sub_session_id,
                entry.parent_session_id,
                extra={"session_id": sub_session_id, "parent_session_id": entry.parent_session_id},
            )
            return
        self._stage_parent_lifecycle_notification(
            sub_session_id,
            f"[子任务 {self._subagent_id(sub_session_id)} 失败]\n错误：{error}",
        )

    def cancel_sub(self, sub_session_id: str) -> bool:
        entry = self._pool.get(sub_session_id) or self._zombies.get(sub_session_id)
        if entry is None:
            logger.warning("CorePool.cancel_sub: unknown session_id=%s", sub_session_id)
            return False
        if entry.sub_status in ("completed", "failed", "cancelled"):
            logger.info(
                "CorePool: cancel no-op session_id=%s already status=%s",
                sub_session_id,
                entry.sub_status,
                extra={"session_id": sub_session_id, "parent_session_id": entry.parent_session_id},
            )
            return True
        previous_status = entry.sub_status
        if entry.bg_task is not None and not entry.bg_task.done():
            entry.bg_task.cancel()
            logger.info(
                "CorePool: cancelled bg_task session_id=%s parent_session_id=%s previous_status=%s",
                sub_session_id,
                entry.parent_session_id,
                previous_status,
                extra={"session_id": sub_session_id, "parent_session_id": entry.parent_session_id, "status": "cancelled"},
            )
        else:
            logger.info(
                "CorePool: marked cancelled (no bg_task or already done) session_id=%s parent_session_id=%s",
                sub_session_id,
                entry.parent_session_id,
                extra={"session_id": sub_session_id, "parent_session_id": entry.parent_session_id, "status": "cancelled"},
            )
        if self._scheduler is not None:
            self._scheduler.cancel_session_tasks(sub_session_id)
        entry.sub_status = "cancelled"
        entry.sub_completed_at = time.time()
        if self._scheduler is not None:
            notify = getattr(self._scheduler, "notify_subagent_terminal_waiter", None)
            if callable(notify):
                notify(sub_session_id)
        return True

    def scan_stale_subagent_zombies(self, ttl_seconds: float) -> List[str]:
        """终态 sub zombie 在 `sub_completed_at`（或 created_at）后超过 ttl_seconds 的 session_id 列表。"""
        if ttl_seconds <= 0:
            return []
        now = time.time()
        stale: List[str] = []
        for sid, entry in self._zombies.items():
            if not sid.startswith("sub:"):
                continue
            st = entry.sub_status
            if st not in ("completed", "failed", "cancelled"):
                continue
            base_ts = entry.sub_completed_at if entry.sub_completed_at is not None else entry.created_at
            if now - float(base_ts) > ttl_seconds:
                stale.append(sid)
        return stale

    def reap_zombie(self, session_id: str) -> None:
        if session_id.startswith("sub:"):
            self.discard_pending_subagent_lifecycle_inject(session_id)
            try:
                from agent_core.agent.workspace_paths import remove_subagent_workspace_trees

                remove_subagent_workspace_trees(self._config.command_tools, session_id)
            except Exception as exc:
                logger.warning(
                    "CorePool.reap_zombie: subagent workspace cleanup failed session_id=%s: %s",
                    session_id,
                    exc,
                    extra={"session_id": session_id},
                )
        self._zombies.pop(session_id, None)
        if session_id.startswith("sub:"):
            self._reaped_subagent_at[session_id] = time.time()
            self._parent_got_terminal_via_wait_tool.discard(session_id)

    def was_subagent_reaped_in_process(
        self, session_id: str, *, max_age_seconds: float = 604800.0
    ) -> bool:
        """本 CorePool 进程内是否曾对该 sub session 成功执行过 reap_zombie（未过期）。"""
        ts = self._reaped_subagent_at.get(session_id)
        if ts is None:
            return False
        if time.time() - ts > max_age_seconds:
            self._reaped_subagent_at.pop(session_id, None)
            return False
        return True

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
            logger.info(
                "CorePool.restore_from_checkpoints: missing %s — skipping restore "
                "(unclean exit or first boot; need graceful KernelScheduler.stop to write it)",
                shutdown_path.name,
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
                from agent_core.kernel_interface.profile import (
                    core_profile_from_checkpoint_dict,
                )

                restored_profile = core_profile_from_checkpoint_dict(ckpt.core_profile)
                await self.acquire(
                    session_id,
                    source=ckpt.source,
                    user_id=ckpt.owner_id,
                    profile=restored_profile,
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
        from agent_core.kernel_interface.profile import core_profile_from_checkpoint_dict
        from .core_logger import CoreLifecycleLogger

        profile_synthesized_here = False
        if profile is None:
            profile_synthesized_here = True
            # sub:* 会话必须与 register_sub / _run_subagent_task 一致使用 default_sub，
            # 否则 subagent 源会被 default_full 误判为 mode=full（工具全开），破坏 P2P 协议。
            if (session_id or "").startswith("sub:"):
                subagent_id = session_id[4:]
                profile = _CoreProfile.default_sub(
                    allowed_tools=None,
                    frontend_id="subagent",
                    dialog_window_id=subagent_id,
                    tools_config=self._config.tools,
                )
            elif source in ("cli", "feishu"):
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
                    tools_config=self._config.tools,
                )

        # 仅在本函数内按 source 推断了 profile时：尝试用 checkpoint 内存档的 CoreProfile 覆盖（新版 JSON）
        if profile_synthesized_here and not (session_id or "").startswith("cron:"):
            try:
                mem_paths = resolve_memory_owner_paths(
                    self._config.memory, user_id, config=self._config, source=source
                )
                disk_ckpt = CoreCheckpointManager(mem_paths["checkpoint_path"]).read()
                if (
                    disk_ckpt is not None
                    and not disk_ckpt.expired
                    and disk_ckpt.session_id == session_id
                    and disk_ckpt.core_profile
                ):
                    recovered = core_profile_from_checkpoint_dict(disk_ckpt.core_profile)
                    if recovered is not None:
                        profile = recovered
            except Exception:
                pass

        # 优先使用 system.tools.build_tool_registry，与 Kernel/MCP 工具装配一致
        from system.tools import build_tool_registry

        reg = build_tool_registry(
            profile=profile,
            config=self._config,
            memory_owner_id=user_id,
            core_pool=self,
        )
        tool_catalog = build_tool_registry(
            profile=profile,
            config=self._config,
            memory_owner_id=user_id,
            core_pool=self,
            filter_by_profile=False,
        )
        tools = list(reg.list_tools()[1].values())
        # search_tools / call_tool 需绑定 AgentCore 自身的 ToolWorkingSetManager；
        # build_tool_registry() 中创建的实例绑定的是外部 ToolWorkingSetManager，
        # 若直接传入 AgentCore 会触发 has("search_tools") 守卫、跳过内部正确版本的创建，
        # 导致 search_tools 更新的工作集被 InternalLoader 忽略。
        # 解决方案：过滤掉这两个工具，AgentCore.__init__ 会用正确的 working_set 重新注册它们。
        tools = [t for t in tools if t.name not in {"search_tools", "call_tool"}]
        # 是否为该 Core 启用本地记忆库：默认跟随配置，
        # 但允许 CoreProfile（如 cron/heartbeat）按 Core 粒度关闭，避免创建一次性 owner 目录。
        memory_enabled = getattr(profile, "memory_enabled", True)

        max_iter = self._config.agent.max_iterations
        if profile is not None and getattr(profile, "max_iterations_override", None) is not None:
            max_iter = profile.max_iterations_override
        agent = AgentCore(
            config=self._config,
            tools=tools,
            tool_catalog=tool_catalog,
            max_iterations=max_iter,
            timezone=self._config.time.timezone,
            user_id=user_id,
            source=source,
            session_logger=None,  # 关闭旧版会话日志，改用 Kernel 级 CoreLifecycleLogger
            memory_enabled=memory_enabled,
            core_profile=profile,
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
                enable_detailed = (
                    getattr(log_cfg, "enable_detailed_log", False) if log_cfg else False
                )
                max_sp_len = (
                    getattr(log_cfg, "max_system_prompt_log_len", 2000)
                    if log_cfg
                    else 2000
                )
                core_logger = CoreLifecycleLogger(
                    base_dir=log_dir,
                    source=source,
                    user_id=user_id,
                    session_id=session_id,
                    enable_detailed_log=enable_detailed,
                    max_system_prompt_log_len=max_sp_len,  # -1 表示不截断
                )
                core_logger.on_core_start(profile=profile)
            except Exception as exc:
                logger.warning(
                    "CorePool: CoreLifecycleLogger creation failed for session=%s: %s",
                    session_id,
                    exc,
                )
                core_logger = None

            # CoreProfile 已在 AgentCore(core_profile=) 传入（供 bash 工作区等在 __aenter__ 前可用）

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

            preferred = self.get_session_preferred_llm_provider(session_id)
            if preferred:
                sw = getattr(agent, "switch_model", None)
                if callable(sw):
                    try:
                        sw(preferred)
                    except Exception as exc:
                        logger.warning(
                            "CorePool._load: apply session preferred LLM %r failed "
                            "for session=%s: %s",
                            preferred,
                            session_id,
                            exc,
                        )

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
            core_pool=self,
        )
        tool_catalog = build_tool_registry(
            profile=profile,
            config=self._config,
            memory_owner_id=user_id,
            core_pool=self,
            filter_by_profile=False,
        )
        from agent_core.tools import VersionedToolRegistry
        reg_tools = list(reg.list_tools()[1].values())
        reg_tools = [t for t in reg_tools if t.name not in {"search_tools", "call_tool"}]
        stripped_reg = VersionedToolRegistry()
        for tool in reg_tools:
            stripped_reg.register(tool)
        entry.agent._tool_registry = stripped_reg
        entry.agent._tool_catalog = tool_catalog
        from agent_core.agent.working_set_pins import compute_pinned_tool_names_for_core
        from agent_core.orchestrator import ToolWorkingSetManager
        from agent_core.tools import CallToolTool, SearchToolsTool

        agent_cfg = getattr(entry.agent, "_config", None) or self._config
        entry.agent._working_set = ToolWorkingSetManager(
            pinned_tools=compute_pinned_tool_names_for_core(
                agent_cfg, profile, entry.agent._source
            ),
            working_set_size=agent_cfg.agent.working_set_size,
        )
        entry.agent._tool_registry.register(
            SearchToolsTool(
                registry=tool_catalog,
                working_set=entry.agent._working_set,
                profile_getter=lambda: getattr(entry.agent, "_core_profile", None),
            )
        )
        entry.agent._tool_registry.register(
            CallToolTool(
                registry=tool_catalog,
                profile_getter=lambda: getattr(entry.agent, "_core_profile", None),
            )
        )
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

    @staticmethod
    def _subagent_id(session_id: str) -> str:
        return session_id[4:] if session_id.startswith("sub:") else session_id

    def _strip_entry_for_zombie(self, entry: CoreEntry) -> CoreEntry:
        zombie = replace(entry, agent=None)
        zombie.bg_task = None
        zombie.last_active_ts = time.monotonic()
        return zombie

    def _inject_to_parent(self, session_id: str, entry: CoreEntry, content: str) -> None:
        from agent_core.kernel_interface.action import AgentMessage, KernelRequest

        if self._scheduler is None:
            logger.warning(
                "CorePool: scheduler not set, cannot inject_turn session_id=%s parent_session_id=%s",
                session_id,
                entry.parent_session_id,
                extra={"session_id": session_id, "parent_session_id": entry.parent_session_id},
            )
            return
        parent_session_id = (entry.parent_session_id or "").strip()
        if not parent_session_id:
            logger.error(
                "CorePool: parent_session_id is empty for session_id=%s — inject_turn aborted",
                session_id,
                extra={"session_id": session_id},
            )
            return
        if session_id.startswith("sub:") and self.was_subagent_reaped_in_process(session_id):
            logger.info(
                "CorePool: skip inject_to_parent (child already reaped) session_id=%s parent_session_id=%s",
                session_id,
                parent_session_id,
                extra={"session_id": session_id, "parent_session_id": parent_session_id},
            )
            return
        msg_type = "task" if entry.sub_status == "completed" else "notify"
        message_id = str(__import__("uuid").uuid4())
        agent_msg = AgentMessage(
            message_id=message_id,
            sender_session=session_id,
            receiver_session=parent_session_id,
            message_type=msg_type,
            subagent_id=self._subagent_id(session_id),
        )
        inject_md: Dict[str, Any] = {METADATA_KEY_AGENT_MESSAGE: agent_msg}
        # 飞书父会话：inject_turn 侧流式卡片 + 工具 trace（与 FeishuIPCBridge 主路径一致）
        if parent_session_id.startswith("feishu:"):
            try:
                from frontend.feishu.feishu_turn_hooks import (
                    FeishuTurnHooksController,
                    resolve_feishu_chat_id_for_session,
                )

                chat_id = resolve_feishu_chat_id_for_session(
                    parent_session_id, core_pool=self
                )
                if chat_id:
                    ctrl = FeishuTurnHooksController(
                        chat_id=chat_id,
                        markdown_header_title="回复",
                    )
                    inject_md["_hooks"] = ctrl.hooks
                    inject_md["_feishu_hook_ctx"] = ctrl
                    inject_md["feishu_chat_id"] = chat_id
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "CorePool: feishu inject hooks skipped parent=%s: %s",
                    parent_session_id,
                    exc,
                )
        request = KernelRequest.create(
            text=content,
            session_id=parent_session_id,
            frontend_id="subagent",
            priority=-1,
            metadata=inject_md,
        )
        logger.info(
            "CorePool: inject_to_parent message_id=%s session_id=%s parent_session_id=%s message_type=%s content_len=%s",
            message_id[:8],
            session_id,
            parent_session_id,
            msg_type,
            len(content),
            extra={
                "session_id": session_id,
                "parent_session_id": parent_session_id,
                "message_id": message_id,
                "message_type": msg_type,
            },
        )
        try:
            self._scheduler.inject_turn(request)
        except Exception as exc:
            logger.warning(
                "CorePool: inject_turn failed session_id=%s parent_session_id=%s error=%s",
                session_id,
                parent_session_id,
                exc,
                extra={"session_id": session_id, "parent_session_id": parent_session_id},
            )
