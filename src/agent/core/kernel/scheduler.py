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
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from agent.core.interfaces import AgentHooks, AgentRunResult

from .action import KernelRequest

if TYPE_CHECKING:
    from agent.core.kernel.kernel import AgentKernel
    from agent.core.kernel.core_pool import CorePool

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
            logger.warning("OutputRouter: no pending future for request_id=%s", request_id)
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
    ) -> None:
        self._kernel = kernel
        self._core_pool = core_pool
        self._hooks_factory = hooks_factory
        self._queue: asyncio.PriorityQueue[KernelRequest] = asyncio.PriorityQueue()
        self._router = OutputRouter()
        self._dispatch_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self._active_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """启动调度器的后台分发循环。"""
        if self._dispatch_task is not None and not self._dispatch_task.done():
            return
        self._stopped.clear()
        self._dispatch_task = asyncio.create_task(
            self._dispatch_loop(), name="kernel-scheduler-dispatch"
        )
        logger.info("KernelScheduler: started")

    async def stop(self) -> None:
        """停止调度器，等待所有活跃任务完成。"""
        self._stopped.set()
        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        # 等待所有活跃的任务完成
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
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

    async def _run_and_route(self, request: KernelRequest) -> None:
        """
        执行单个请求并将结果路由到对应 Future。

        1. 从 CorePool 获取对应 session 的 AgentCore
        2. 调用 AgentKernel.run() 驱动 AgentCore
        3. 通过 OutputRouter 将结果回传给前端
        """
        try:
            # 准备钩子
            hooks = None
            if self._hooks_factory:
                hooks = self._hooks_factory(request)

            # 获取 AgentCore（懒加载）
            source = request.metadata.get("source", request.frontend_id)
            user_id = request.metadata.get("user_id", "root")
            agent = await self._core_pool.acquire(
                request.session_id,
                source=source,
                user_id=user_id,
            )

            # 准备本轮输入（注入到 agent 的上下文）
            await agent._sync_external_session_updates()
            agent._current_turn_id += 1
            turn_id = agent._current_turn_id

            content_items = request.metadata.get("content_items")
            agent._context.add_user_message(request.text)
            agent._outgoing_attachments.clear()
            if content_items:
                agent._pending_multimodal_items.extend(content_items)

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

            # 驱动 AgentCore
            run_result = await self._kernel.run(agent, turn_id=turn_id, hooks=hooks)

            # 后处理
            await agent._finalize_turn(run_result, summary_task, summary_recent_start)

            # 路由结果
            await self._router.deliver(request.request_id, run_result)

        except Exception as exc:
            logger.exception(
                "KernelScheduler: error processing request_id=%s: %s",
                request.request_id[:8],
                exc,
            )
            await self._router.deliver_error(request.request_id, exc)

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def active_task_count(self) -> int:
        return len(self._active_tasks)

    @property
    def router(self) -> OutputRouter:
        return self._router
