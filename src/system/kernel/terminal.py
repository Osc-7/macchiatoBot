"""
KernelTerminal — Kernel 系统管理控制台。

不绑定任何 session，直接操作 Kernel 内部组件（CorePool / Scheduler）。
类比 OS terminal：ps / top / kill / inspect 等系统级命令。

使用方式
--------
1) 在 daemon 进程内（已有引用时）::

    terminal = KernelTerminal(scheduler=scheduler_runtime, core_pool=core_pool)
    for c in terminal.ps():
        print(c.session_id, c.idle_seconds)
    status = terminal.top()
    await terminal.kill("cli:old-session")

2) 从外部进程（CLI / 脚本）通过 IPC::

    from system.automation import AutomationIPCClient

    client = AutomationIPCClient()
    await client.connect()
    cores = await client.terminal_ps()
    status = await client.terminal_top()
    await client.terminal_kill("shuiyuan:SomeUser")
    result = await client.terminal_attach("cli:root", "系统通知：即将重启")
    cron = await client.terminal_automation_jobs()  # 正在被追踪的定时 Job 协程
    tasks = await client.terminal_agent_tasks()     # AgentTask 队列快照

  前提：daemon 已启动且 AutomationIPCServer 注入了 terminal（当前 automation_daemon 已注入）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from system.automation.scheduler import AutomationScheduler
    from system.automation.task_queue import AgentTaskQueue

    from .core_pool import CorePool
    from .scheduler import KernelScheduler

logger = __import__("logging").getLogger(__name__)


@dataclass
class CoreInfo:
    """单个 Core 的快照信息（ps 命令的一行）。"""

    session_id: str
    source: str
    user_id: str
    mode: str  # full / sub / background
    uptime_seconds: float
    idle_seconds: float
    ttl_remaining_seconds: float
    turn_count: int
    total_tokens: int
    memory_enabled: bool


@dataclass
class SystemStatus:
    """系统整体快照（top 命令的输出）。"""

    active_cores: int
    max_cores: int
    queue_depth: int
    inflight_tasks: int
    uptime_seconds: float


@dataclass
class SessionDetail:
    """单个 session 的详细信息（inspect 命令的输出）。"""

    session_id: str
    source: str
    user_id: str
    mode: str
    profile_summary: Dict[str, Any]
    uptime_seconds: float
    idle_seconds: float
    ttl_remaining_seconds: float
    turn_count: int
    token_usage: Dict[str, int]
    context_message_count: int
    memory_enabled: bool
    has_checkpoint: bool
    log_file: Optional[str]


class KernelTerminal:
    """
    Kernel 系统控制台 — 内核的管理面板。

    不绑定任何 session，直接操作 Kernel 内部组件。
    类比 OS terminal：通过命令查看系统状态、管理进程。

    所有查询方法为同步；kill / spawn / cancel / attach 为异步系统调用。
    """

    def __init__(
        self,
        scheduler: "KernelScheduler",
        core_pool: "CorePool",
        *,
        boot_time: Optional[float] = None,
        automation_scheduler: Optional["AutomationScheduler"] = None,
        agent_task_queue: Optional["AgentTaskQueue"] = None,
    ) -> None:
        self._scheduler = scheduler
        self._pool = core_pool
        self._boot_time = boot_time if boot_time is not None else time.monotonic()
        self._automation_scheduler = automation_scheduler
        self._agent_task_queue = agent_task_queue

    # ── 查询类：只读，不改变系统状态 ────────────────────────────

    def ps(self) -> List[CoreInfo]:
        """
        列出所有活跃 Core（类比 ps aux）。

        数据源：CorePool._pool 中所有 CoreEntry。
        """
        now = time.monotonic()
        result: List[CoreInfo] = []
        for session_id, entry in self._pool._pool.items():
            agent = entry.agent
            profile = entry.profile
            source = getattr(agent, "_source", "cli")
            user_id = getattr(agent, "_user_id", "root")
            mode = getattr(profile, "mode", "full")
            ttl_sec = getattr(profile, "session_expired_seconds", 1800)
            uptime = now - entry.session_start_ts
            idle = now - entry.last_active_ts
            ttl_remaining = max(0.0, ttl_sec - idle)

            turn_count = 0
            total_tokens = 0
            fn_turn = getattr(agent, "get_turn_count", None)
            if callable(fn_turn):
                try:
                    turn_count = int(fn_turn())
                except Exception:
                    pass
            fn_usage = getattr(agent, "get_token_usage", None)
            if callable(fn_usage):
                try:
                    u = fn_usage()
                    if isinstance(u, dict):
                        total_tokens = int(u.get("total_tokens", 0))
                except Exception:
                    pass

            memory_enabled = getattr(profile, "memory_enabled", True)
            result.append(
                CoreInfo(
                    session_id=session_id,
                    source=source,
                    user_id=user_id,
                    mode=mode,
                    uptime_seconds=uptime,
                    idle_seconds=idle,
                    ttl_remaining_seconds=ttl_remaining,
                    turn_count=turn_count,
                    total_tokens=total_tokens,
                    memory_enabled=memory_enabled,
                )
            )
        return result

    def top(self) -> SystemStatus:
        """
        系统概览（类比 top / htop）。

        返回：活跃 Core 数、队列深度、inflight 任务数、uptime。
        """
        active = len(self._pool._pool)
        max_cores = self._pool._max_sessions
        queue_depth = self._scheduler.queue_size
        inflight = self._scheduler.active_task_count
        uptime = time.monotonic() - self._boot_time
        return SystemStatus(
            active_cores=active,
            max_cores=max_cores,
            queue_depth=queue_depth,
            inflight_tasks=inflight,
            uptime_seconds=uptime,
        )

    def inspect(self, session_id: str) -> SessionDetail:
        """
        查看指定 Core 的详细信息（类比 /proc/<pid>/status）。

        包含 profile 配置、token 用量、上下文消息数、日志路径等。
        """
        entry = self._pool.get_entry(session_id)
        if entry is None:
            raise KeyError(f"session not found: {session_id}")

        agent = entry.agent
        profile = entry.profile
        now = time.monotonic()
        source = getattr(agent, "_source", "cli")
        user_id = getattr(agent, "_user_id", "root")
        mode = getattr(profile, "mode", "full")
        ttl_sec = getattr(profile, "session_expired_seconds", 1800)
        uptime = now - entry.session_start_ts
        idle = now - entry.last_active_ts
        ttl_remaining = max(0.0, ttl_sec - idle)

        turn_count = 0
        fn_turn = getattr(agent, "get_turn_count", None)
        if callable(fn_turn):
            try:
                turn_count = int(fn_turn())
            except Exception:
                pass

        token_usage: Dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        fn_usage = getattr(agent, "get_token_usage", None)
        if callable(fn_usage):
            try:
                u = fn_usage()
                if isinstance(u, dict):
                    token_usage["prompt_tokens"] = int(u.get("prompt_tokens", 0))
                    token_usage["completion_tokens"] = int(u.get("completion_tokens", 0))
                    token_usage["total_tokens"] = int(u.get("total_tokens", 0))
            except Exception:
                pass

        context_message_count = 0
        ctx = getattr(agent, "_context", None)
        if ctx is not None:
            get_msgs = getattr(ctx, "get_messages", None)
            if callable(get_msgs):
                try:
                    context_message_count = len(get_msgs())
                except Exception:
                    pass

        profile_summary: Dict[str, Any] = {
            "mode": mode,
            "session_expired_seconds": ttl_sec,
            "memory_enabled": getattr(profile, "memory_enabled", True),
            "max_context_tokens": getattr(profile, "max_context_tokens", 80_000),
        }

        has_checkpoint = False
        ckpt_mgr = getattr(agent, "_checkpoint_manager", None)
        if ckpt_mgr is not None:
            path = getattr(ckpt_mgr, "_path", None)
            if path is not None and Path(path).exists():
                has_checkpoint = True

        log_file: Optional[str] = None
        log_obj = getattr(entry, "logger", None)
        if log_obj is not None:
            fp = getattr(log_obj, "file_path", None)
            if fp is not None:
                log_file = str(fp)

        return SessionDetail(
            session_id=session_id,
            source=source,
            user_id=user_id,
            mode=mode,
            profile_summary=profile_summary,
            uptime_seconds=uptime,
            idle_seconds=idle,
            ttl_remaining_seconds=ttl_remaining,
            turn_count=turn_count,
            token_usage=token_usage,
            context_message_count=context_message_count,
            memory_enabled=profile_summary["memory_enabled"],
            has_checkpoint=has_checkpoint,
            log_file=log_file,
        )

    def queue(self) -> Dict[str, Any]:
        """
        查看 Scheduler 队列状态。

        返回：队列大小、inflight session 分布、cancelled sessions 列表。
        """
        inflight = dict(self._scheduler._inflight_sessions)
        inflight = {k: v for k, v in inflight.items() if v > 0}
        cancelled = list(self._scheduler._cancelled_sessions)
        return {
            "queue_size": self._scheduler.queue_size,
            "inflight_sessions": inflight,
            "cancelled_sessions": cancelled,
            "active_task_count": self._scheduler.active_task_count,
        }

    def automation_tracked_jobs(self) -> Dict[str, Any]:
        """
        查看 AutomationScheduler 在进程内为每个 JobDefinition 维护的调度协程（类比 cron -l + 进程表）。

        仅在 automation_daemon 等注入 ``automation_scheduler`` 时可用；否则返回 available=False。
        """
        if self._automation_scheduler is None:
            return {
                "available": False,
                "message": "未注入 AutomationScheduler（完整能力需在 automation_daemon 下使用）",
            }
        snap = self._automation_scheduler.tracked_jobs_snapshot()
        return {"available": True, **snap}

    def agent_task_queue_status(self, *, limit: int = 25) -> Dict[str, Any]:
        """
        查看 SQLite 持久化队列中的定时/后台 AgentTask：pending/running 数量与最近条目。

        仅在注入 ``agent_task_queue`` 时可用。
        """
        if self._agent_task_queue is None:
            return {
                "available": False,
                "message": "未注入 AgentTaskQueue",
            }
        q = self._agent_task_queue
        recent = q.list_recent(limit=max(1, min(limit, 100)))
        items: List[Dict[str, Any]] = []
        for t in recent:
            row = t.model_dump(mode="json")
            instr = str(row.get("instruction") or "")
            if len(instr) > 240:
                row["instruction"] = instr[:240] + "…"
            items.append(row)
        return {
            "available": True,
            "pending_count": q.pending_count(),
            "running_count": q.running_count(),
            "recent_tasks": items,
        }

    # ── 系统调用：改变系统状态 ────────────────────────────────

    async def spawn(
        self,
        session_id: str,
        *,
        source: str = "system",
        user_id: str = "root",
        profile: Optional[Any] = None,
    ) -> CoreInfo:
        """
        创建新 Core（类比 fork+exec）。

        通过 CorePool.acquire() 加载新的 AgentCore 实例。
        """
        await self._pool.acquire(
            session_id,
            source=source,
            user_id=user_id,
            create_if_missing=True,
            profile=profile,
        )
        infos = self.ps()
        for info in infos:
            if info.session_id == session_id:
                return info
        raise RuntimeError(f"spawn succeeded but session not in pool: {session_id}")

    async def kill(self, session_id: str) -> None:
        """
        终结指定 Core（类比 kill -9）。

        通过 CorePool.evict() 执行完整的 kill 流程：
        收集 CoreStats → 写入长期记忆摘要 → close 释放资源。
        """
        await self._pool.evict(session_id, shutdown=False)

    async def cancel(self, session_id: str) -> bool:
        """
        取消指定 session 的正在运行的任务（类比 kill -INT）。

        通过 Scheduler.cancel_session_tasks() 取消 inflight 任务，
        但不销毁 Core 本身。
        """
        return self._scheduler.cancel_session_tasks(session_id)

    # ── 交互类：attach 到某个 session 直接对话 ─────────────────

    async def attach(
        self,
        session_id: str,
        text: str,
        *,
        hooks: Optional[Any] = None,
    ) -> Any:
        """
        以系统身份向指定 session 发送一条消息（类比 screen -r）。

        构造 KernelRequest 提交到 Scheduler，等待结果返回。
        这是唯一涉及 LLM 的方法。
        """
        from agent_core.kernel_interface import KernelRequest

        metadata: Dict[str, Any] = {"source": "system", "user_id": "root"}
        if hooks is not None:
            metadata["_hooks"] = hooks
        request = KernelRequest.create(
            text=text,
            session_id=session_id,
            frontend_id="system",
            metadata=metadata,
        )
        handle = await self._scheduler.submit(request)
        return await self._scheduler.wait_result(handle)
