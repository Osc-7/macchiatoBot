"""
KernelScheduler — 单线程异步调度器。

包含两个核心子组件：
1. OutputRouter  — 类比 OS 文件描述符表，request_id → asyncio.Future，实现"乱序完成精准路由"
2. KernelScheduler — 类比 OS 进程调度器，InputQueue + dispatch_loop + create_task 实现跨 session 真并发

设计原则：
- asyncio.PriorityQueue 按 (priority, enqueued_at) 排序，高优先级先处理，同优先级 FIFO
- _dispatch_loop 使用 create_task，不 await 任务本身，让多个 session 的 IO 真正并发（协作式）
- OutputRouter 用 Future 而非输出队列，前端 await future 挂起等待，无需轮询
- "乱序完成"自动实现：快速任务先解锁对应 Future，慢任务继续在后台运行
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Optional

from agent_core.interfaces import AgentHooks, AgentRunResult
from agent_core.kernel_interface import KernelRequest

if TYPE_CHECKING:
    from .kernel import AgentKernel
    from .core_pool import CorePool, CoreEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OutputRouter — 结果路由器
# ---------------------------------------------------------------------------


class OutputRouter:
    """
    结果路由器，类比 OS 文件描述符表。

    前端 submit 时注册一个 Future，Kernel 完成时 deliver() 解锁，
    前端 await future 拿到结果。支持乱序完成（O(1) 路由）。
    """

    def __init__(self) -> None:
        self._pending: Dict[str, asyncio.Future] = {}

    def register(self, request_id: str) -> asyncio.Future:
        """
        注册一个 request_id，返回对应的 Future。
        前端 await 这个 Future 即可等待结果。
        """
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[request_id] = fut
        return fut

    async def deliver(self, request_id: str, result: AgentRunResult) -> None:
        """Kernel 完成时调用，将结果设置到对应 Future 上。"""
        fut = self._pending.pop(request_id, None)
        if fut is None:
            logger.warning(
                "OutputRouter: no pending future for request_id=%s", request_id
            )
            return
        if not fut.done():
            fut.set_result(result)

    async def deliver_error(self, request_id: str, exc: BaseException) -> None:
        """Kernel 出错时调用，将异常设置到 Future 上。"""
        fut = self._pending.pop(request_id, None)
        if fut is None:
            return
        if not fut.done():
            fut.set_exception(exc)

    def cancel_all(self) -> None:
        """关闭时取消所有挂起的 Future。"""
        for request_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    @property
    def pending_count(self) -> int:
        return len(self._pending)


# ---------------------------------------------------------------------------
# KernelScheduler — 调度器
# ---------------------------------------------------------------------------


class KernelScheduler:
    """
    单线程异步调度器，类比 OS 进程调度器。

    - submit(): 将 KernelRequest 投入优先级队列，返回 Future（前端 await 等待结果）
    - _dispatch_loop(): 消费队列，每个请求 create_task 独立运行（跨 session 真并发）
    - 乱序完成：快任务先完成并通过 OutputRouter 路由，慢任务继续后台执行

    Usage::

        scheduler = KernelScheduler(kernel=kernel, core_pool=core_pool)
        await scheduler.start()
        future = await scheduler.submit(KernelRequest.create(text="...", session_id="..."))
        result = await future      # 等待 Kernel 完成
        await scheduler.stop()
    """

    def __init__(
        self,
        kernel: "AgentKernel",
        core_pool: "CorePool",
        *,
        hooks_factory: Optional[Callable[[KernelRequest], AgentHooks]] = None,
        ttl_scan_interval: float = 30.0,
    ) -> None:
        self._kernel = kernel
        self._core_pool = core_pool
        self._hooks_factory = hooks_factory
        self._ttl_scan_interval = ttl_scan_interval
        self._queue: asyncio.PriorityQueue[KernelRequest] = asyncio.PriorityQueue()
        self._router = OutputRouter()
        self._dispatch_task: Optional[asyncio.Task] = None
        self._ttl_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self._active_tasks: set[asyncio.Task] = set()
        # session_id -> in-flight request count
        self._inflight_sessions: Dict[str, int] = defaultdict(int)

    async def start(self) -> None:
        """启动调度循环和 TTL 扫描后台任务。

        启动前先执行进程表重建：扫描 checkpoint.json，将上次 kernel 关闭前
        未过期的 Core 恢复到 pool 中，再交由 TTL 循环接管生命周期监控。
        """
        if self._dispatch_task is not None and not self._dispatch_task.done():
            return
        self._stopped.clear()

        # 进程表重建：扫描 checkpoints，恢复未过期 Core
        try:
            restored = await self._core_pool.restore_from_checkpoints()
            if restored:
                logger.info(
                    "KernelScheduler: restored %d session(s) from checkpoint", restored
                )
        except Exception as exc:
            logger.warning(
                "KernelScheduler: restore_from_checkpoints failed: %s", exc
            )

        self._dispatch_task = asyncio.create_task(
            self._dispatch_loop(), name="kernel-scheduler-dispatch"
        )
        self._ttl_task = asyncio.create_task(
            self._ttl_loop(), name="kernel-scheduler-ttl"
        )
        logger.info(
            "KernelScheduler: started (ttl_scan_interval=%.0fs)",
            self._ttl_scan_interval,
        )

    async def stop(self) -> None:
        """停止调度器，等待所有活跃任务完成。"""
        self._stopped.set()
        for task in (self._dispatch_task, self._ttl_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        # 关闭前写入 kernel 关闭时间戳，供下次启动用「关闭时间 - checkpoint 时间」判断是否过期
        try:
            from agent_core.agent.memory_paths import get_kernel_shutdown_at_path
            path = get_kernel_shutdown_at_path(self._core_pool._config.memory)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(str(time.time()), encoding="utf-8")
        except Exception as exc:
            logger.warning("KernelScheduler: write kernel_last_shutdown_at failed: %s", exc)
        # Kernel 级停止时，确保回收所有仍在 CorePool 中的会话，避免遗留 active Core。
        try:
            await self._core_pool.evict_all()
        except Exception as exc:
            logger.warning("KernelScheduler: evict_all on stop failed: %s", exc)
        self._router.cancel_all()
        logger.info("KernelScheduler: stopped")

    async def submit(
        self,
        request: KernelRequest,
    ) -> "asyncio.Future[AgentRunResult]":
        """
        将请求投入队列，返回 Future。

        前端 await 返回的 Future 即可等待结果（不阻塞调度器）。
        乱序完成：不同 session 的请求并发执行，先完成的先解锁 Future。
        """
        fut = self._router.register(request.request_id)
        await self._queue.put(request)
        logger.debug(
            "KernelScheduler: queued request_id=%s session=%s priority=%d",
            request.request_id[:8],
            request.session_id,
            request.priority,
        )
        return fut

    async def _dispatch_loop(self) -> None:
        """
        分发循环主体。

        从优先级队列取出请求，每个请求 create_task 独立执行，
        不 await 任务本身，确保多 session 并发不互相阻塞。
        """
        while not self._stopped.is_set():
            try:
                request = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            task = asyncio.create_task(
                self._run_and_route(request),
                name=f"kernel-req-{request.request_id[:8]}",
            )
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)

    async def _ttl_loop(self) -> None:
        """
        TTL 扫描后台循环。

        每隔 ttl_scan_interval 秒扫描一次 CorePool，
        将超过 session_expired_seconds 的 Core 触发 evict 流程。
        """
        while not self._stopped.is_set():
            try:
                await asyncio.sleep(self._ttl_scan_interval)
            except asyncio.CancelledError:
                break
            await self._evict_expired()

    async def _evict_expired(self) -> None:
        """扫描并驱逐所有超时 session。"""
        expired = self._core_pool.scan_expired()
        if not expired:
            return
        runnable = [sid for sid in expired if self._inflight_sessions.get(sid, 0) <= 0]
        skipped = [sid for sid in expired if sid not in runnable]
        if skipped:
            logger.debug(
                "KernelScheduler: skip TTL evict for in-flight session(s): %s",
                skipped,
            )
        if not runnable:
            return
        logger.info(
            "KernelScheduler: TTL scan found %d expired session(s), evicting %d: %s",
            len(expired),
            len(runnable),
            runnable,
        )
        for session_id in runnable:
            try:
                await self._core_pool.evict(session_id)
                logger.info("KernelScheduler: evicted expired session %s", session_id)
            except Exception as exc:
                logger.warning(
                    "KernelScheduler: evict failed (session=%s): %s", session_id, exc
                )

    async def _run_and_route(self, request: KernelRequest) -> None:
        """
        执行单个请求并将结果路由到对应 Future。

        1. 从 CorePool 获取对应 session 的 AgentCore
        2. 调用 AgentKernel.run() 驱动 AgentCore
        3. 通过 OutputRouter 将结果回传给前端
        """
        session_id = request.session_id
        self._inflight_sessions[session_id] += 1
        try:
            # 准备钩子
            hooks = None
            if self._hooks_factory:
                hooks = self._hooks_factory(request)
            elif isinstance(request.metadata, dict):
                # 允许调用方通过 request.metadata["_hooks"] 直接透传运行时回调，
                # 方便在不定制 hooks_factory 的情况下复用 trace/stream 通路。
                raw_hooks = request.metadata.get("_hooks")
                if isinstance(raw_hooks, AgentHooks):
                    hooks = raw_hooks

            # 获取 AgentCore（懒加载）
            # 记忆路径：profile 有非空 frontend_id/dialog_window_id 时优先使用，确保 Cron 等写入对应前端记忆
            profile = request.profile
            mem_source = (
                profile.frontend_id
                if (profile and (profile.frontend_id or "").strip())
                else request.metadata.get("source", request.frontend_id)
            )
            mem_user_id = (
                profile.dialog_window_id
                if (profile and (profile.dialog_window_id or "").strip())
                else request.metadata.get("user_id", "root")
            )
            agent = await self._core_pool.acquire(
                request.session_id,
                source=mem_source,
                user_id=mem_user_id,
                profile=profile,
            )

            # 准备本轮输入（注入到 agent 的上下文）
            await agent._sync_external_session_updates()
            agent._current_turn_id += 1
            turn_id = agent._current_turn_id

            # Core 级生命周期日志：记录本轮输入
            entry: Optional[CoreEntry] = self._core_pool.get_entry(session_id)  # type: ignore[assignment]
            core_logger = getattr(entry, "logger", None) if entry is not None else None
            if core_logger is not None:
                try:
                    core_logger.on_turn_start(turn_id, request.text)
                except Exception:
                    pass

            content_items = request.metadata.get("content_items")
            if content_items:
                logger.info(
                    "scheduler: injecting %d content_items into LLM context for session=%s (types=%s)",
                    len(content_items),
                    session_id,
                    [str(i.get("type")) for i in content_items[:3]],
                )
            agent._context.add_user_message(
                request.text, media_items=content_items or None
            )
            agent._outgoing_attachments.clear()

            # session_logger 写入用户消息
            if agent._session_logger:
                agent._session_logger.on_user_message(turn_id, request.text)
            if agent._memory_enabled:
                msg_id = agent._chat_history_db.write_message(
                    session_id=agent._session_id,
                    role="user",
                    content=request.text,
                    source=agent._source,
                )
                agent._last_history_id = max(agent._last_history_id, int(msg_id))

            # 启动工作记忆并行总结（如需）
            summary_task = None
            summary_recent_start = None
            if agent._memory_enabled and agent._working_memory.check_threshold(
                actual_tokens=agent._last_prompt_tokens
            ):
                result = agent._working_memory.start_summarize(
                    agent._summary_llm_client, actual_tokens=agent._last_prompt_tokens
                )
                if result:
                    summary_task, summary_recent_start = result

            # 驱动 AgentCore（on_signal: 每次 Return/Tool_call 时刷新 TTL）
            def _on_signal() -> None:
                self._core_pool.touch(session_id)

            run_result = await self._kernel.run(
                agent,
                turn_id=turn_id,
                hooks=hooks,
                on_signal=_on_signal,
            )

            # 后处理
            await agent._finalize_turn(run_result, summary_task, summary_recent_start)

            # Core 级生命周期日志：记录本轮输出
            if core_logger is not None:
                try:
                    core_logger.on_turn_end(
                        turn_id,
                        output_text=run_result.output_text,
                        metadata=run_result.metadata,
                    )
                except Exception:
                    pass

            # 刷新 TTL（每次请求完成后更新活跃时间）
            self._core_pool.touch(session_id)

            # 路由结果
            await self._router.deliver(request.request_id, run_result)

        except Exception as exc:
            logger.exception(
                "KernelScheduler: error processing request_id=%s: %s",
                request.request_id[:8],
                exc,
            )
            await self._router.deliver_error(request.request_id, exc)
        finally:
            pending = self._inflight_sessions.get(session_id, 0) - 1
            if pending > 0:
                self._inflight_sessions[session_id] = pending
            else:
                self._inflight_sessions.pop(session_id, None)

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def active_task_count(self) -> int:
        return len(self._active_tasks)

    @property
    def router(self) -> OutputRouter:
        return self._router

    @property
    def core_pool(self) -> "CorePool":
        """暴露 CorePool 供网关读取内核态 session 状态。"""
        return self._core_pool
