"""
KernelScheduler — 单线程异步调度器。

包含两个核心子组件：
1. OutputBus      — 结果总线，统一负责结果广播与 request_id 级别的等待
2. KernelScheduler — 类比 OS 进程调度器，InputQueue + dispatch_loop + create_task 实现跨 session 真并发

设计原则：
- asyncio.PriorityQueue 按 (priority, enqueued_at) 排序，高优先级先处理，同优先级 FIFO
- _dispatch_loop 使用 create_task，不 await 任务本身，让多个 session 的 IO 真正并发（协作式）
- submit() 语义收敛为「仅提交到 [in] 队列」，返回 request_id
- 所有结果统一经 OutputBus 广播；需要同步等待的一方按 request_id await
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Union
import uuid

from agent_core.interfaces import AgentHooks, AgentRunResult
from agent_core.kernel_interface import KernelRequest

if TYPE_CHECKING:
    from .kernel import AgentKernel
    from .core_pool import CorePool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OutputBus — 输出消息总线
# ---------------------------------------------------------------------------


class OutputBus:
    """
    输出消息总线。

    提供两类能力：
    1) request_id 级别等待：submit 后可 await 对应结果
    2) session 级别广播：结果可广播给订阅者（供推送前端使用）
    """

    def __init__(self) -> None:
        self._pending: Dict[str, asyncio.Future[AgentRunResult]] = {}
        self._listeners: Dict[str, Dict[str, Callable[[str, AgentRunResult], Any]]] = defaultdict(dict)

    def register_waiter(self, request_id: str) -> None:
        """注册一个 request_id 的等待位。"""
        loop = asyncio.get_running_loop()
        self._pending[request_id] = loop.create_future()

    async def wait_result(
        self, request_id: str, timeout_seconds: Optional[float] = None
    ) -> AgentRunResult:
        """等待 request_id 对应结果。"""
        fut = self._pending.get(request_id)
        if fut is None:
            raise RuntimeError(f"request_id not registered: {request_id}")
        if timeout_seconds is None:
            result = await fut
        else:
            result = await asyncio.wait_for(fut, timeout=float(timeout_seconds))
        return result

    def subscribe(
        self, session_id: str, callback: Callable[[str, AgentRunResult], Any]
    ) -> str:
        """订阅某个 session 的输出广播，返回 subscription_id。"""
        sub_id = str(uuid.uuid4())[:12]
        self._listeners[session_id][sub_id] = callback
        return sub_id

    def unsubscribe(self, session_id: str, subscription_id: str) -> None:
        """取消订阅。"""
        listeners = self._listeners.get(session_id)
        if not listeners:
            return
        listeners.pop(subscription_id, None)
        if not listeners:
            self._listeners.pop(session_id, None)

    async def publish(self, session_id: str, request_id: str, result: AgentRunResult) -> None:
        """发布一个结果到总线：先解锁等待者，再广播给订阅者。"""
        fut = self._pending.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(result)
        listeners = list(self._listeners.get(session_id, {}).values())
        for cb in listeners:
            try:
                maybe = cb(request_id, result)
                if asyncio.iscoroutine(maybe):
                    asyncio.create_task(maybe)
            except Exception:
                continue

    async def publish_error(self, request_id: str, exc: BaseException) -> None:
        """向 request_id 对应等待者发布错误。"""
        fut = self._pending.pop(request_id, None)
        if fut is None:
            return
        if not fut.done():
            fut.set_exception(exc)

    def cancel_all(self) -> None:
        """关闭时取消所有挂起等待。"""
        for request_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    def has_waiter(self, request_id: str) -> bool:
        """判断指定 request_id 是否有对应的等待者。"""
        return request_id in self._pending

    @property
    def pending_count(self) -> int:
        return len(self._pending)


class SubmitHandle:
    """submit 返回句柄：可取 request_id，也可直接 await 结果。"""

    def __init__(self, request_id: str, scheduler: "KernelScheduler") -> None:
        self.request_id = request_id
        self._scheduler = scheduler

    def __await__(self):
        return self._scheduler.wait_result(self.request_id).__await__()


# ---------------------------------------------------------------------------
# KernelScheduler — 调度器
# ---------------------------------------------------------------------------


class KernelScheduler:
    """
    单线程异步调度器，类比 OS 进程调度器。

    - submit(): 将 KernelRequest 投入优先级队列，返回 request_id
    - _dispatch_loop(): 消费队列，每个请求 create_task 独立运行（跨 session 真并发）
    - 乱序完成：快任务先完成先产出到 OutputBus，慢任务继续后台执行

    Usage::

        scheduler = KernelScheduler(kernel=kernel, core_pool=core_pool)
        await scheduler.start()
        request_id = await scheduler.submit(KernelRequest.create(text="...", session_id="..."))
        result = await scheduler.wait_result(request_id)  # 等待该请求完成
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
        self._out_bus = OutputBus()
        self._dispatch_task: Optional[asyncio.Task] = None
        self._ttl_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self._active_tasks: set[asyncio.Task] = set()
        # session_id -> in-flight request count（用于阻止 TTL 驱逐运行中的 session）
        self._inflight_sessions: Dict[str, int] = defaultdict(int)
        # per-session 串行化锁：防止同一 session 的并发请求竞争 context/turn_id/DB 写入
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._session_locks_meta: asyncio.Lock = asyncio.Lock()
        # per-session 推送队列：存放 inject_turn 产生的 AgentRunResult，供前端轮询（兼容旧接口）
        self._push_queues: Dict[str, asyncio.Queue] = {}
        # session_id -> 当前正在运行的 _run_and_route Task（用于 cancel_session_tasks）
        self._session_active_task: Dict[str, asyncio.Task] = {}
        # 已取消的 session_id，用于拦截仍在队列中尚未 dispatch 的请求
        self._cancelled_sessions: set[str] = set()

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
        # cancel_all() 必须在 try/finally 中，确保即使 evict_all() 抛出异常也能执行，
        # 否则所有挂起 Future 将永久悬挂。
        try:
            await self._core_pool.evict_all()
        except Exception as exc:
            logger.warning("KernelScheduler: evict_all on stop failed: %s", exc)
        finally:
            self._out_bus.cancel_all()
        logger.info("KernelScheduler: stopped")

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """
        获取指定 session 的串行化锁，不存在时懒创建。

        同一 session 的并发请求在此排队，确保每次只有一个请求驱动 AgentCore，
        防止 context / turn_id / ChatHistoryDB 写入竞争。
        锁对象不主动清理（引用由 dict 持有），session 数量受 max_sessions 约束，内存开销可控。
        """
        async with self._session_locks_meta:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = asyncio.Lock()
            return self._session_locks[session_id]

    async def submit(
        self,
        request: KernelRequest,
    ) -> SubmitHandle:
        """
        将请求投入 [in] 队列，返回 request_id。

        submit 只表达「提交」，不阻塞等待。
        若调用方要同步等待，可调用 wait_result(request_id)。
        """
        if isinstance(request.metadata, dict):
            request.metadata["_submit"] = True
        self._out_bus.register_waiter(request.request_id)
        await self._queue.put(request)
        logger.debug(
            "KernelScheduler: queued request_id=%s session=%s priority=%d",
            request.request_id[:8],
            request.session_id,
            request.priority,
        )
        return SubmitHandle(request.request_id, self)

    async def wait_result(
        self, request_id: Union[str, SubmitHandle], timeout_seconds: Optional[float] = None
    ) -> AgentRunResult:
        """等待指定 request_id 的执行结果。"""
        rid = request_id.request_id if isinstance(request_id, SubmitHandle) else request_id
        return await self._out_bus.wait_result(rid, timeout_seconds=timeout_seconds)

    def subscribe_out(
        self, session_id: str, callback: Callable[[str, AgentRunResult], Any]
    ) -> str:
        """订阅指定 session 的输出广播。"""
        return self._out_bus.subscribe(session_id, callback)

    def unsubscribe_out(self, session_id: str, subscription_id: str) -> None:
        """取消输出广播订阅。"""
        self._out_bus.unsubscribe(session_id, subscription_id)

    def inject_turn(self, request: KernelRequest) -> None:
        """
        注入一个不等待结果的 fire-and-forget 请求。

        与 submit() 的区别：
        - 不注册 request 等待位（调用方无需 await）
        - 直接 put_nowait 入队（优先级默认 -1，高于普通请求）
        - 完成后只广播到 OutputBus，并入 push 队列供 poll_push（兼容）

        典型用途：
        - SubagentRegistry.on_complete/on_fail 唤醒父 session（first-done 语义）
        - SendMessageToAgentTool / ReplyToMessageTool 的 P2P 消息投递
        """
        if isinstance(request.metadata, dict):
            request.metadata["_submit"] = False
        self._queue.put_nowait(request)
        text_len = len(request.text or "")
        source = (request.metadata or {}).get("source", request.frontend_id or "")
        logger.info(
            "KernelScheduler: inject_turn enqueued request_id=%s session_id=%s source=%s text_len=%s",
            request.request_id[:8],
            request.session_id,
            source,
            text_len,
            extra={"request_id": request.request_id, "session_id": request.session_id},
        )

    def cancel_session_tasks(self, session_id: str) -> bool:
        """取消指定 session 的活跃执行任务并标记为已取消。

        同时处理两种情况：
        - 请求已在执行 (_session_active_task) -> 直接 cancel Task
        - 请求仍在队列中等待 dispatch -> 加入 _cancelled_sessions，
          _run_and_route 开头会检查并跳过
        """
        self._cancelled_sessions.add(session_id)
        task = self._session_active_task.get(session_id)
        if task and not task.done():
            task.cancel()
            logger.info(
                "KernelScheduler: cancelled active task for session_id=%s",
                session_id,
            )
            return True
        logger.info(
            "KernelScheduler: marked session_id=%s as cancelled (no active task)",
            session_id,
        )
        return False

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
            self._session_active_task[request.session_id] = task

            def _cleanup_session(t: asyncio.Task, sid: str = request.session_id) -> None:
                self._active_tasks.discard(t)
                if self._session_active_task.get(sid) is t:
                    self._session_active_task.pop(sid, None)

            task.add_done_callback(_cleanup_session)
            # task_done() 使 queue.join() 能正确追踪完成状态
            task.add_done_callback(lambda _: self._queue.task_done())

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
        执行单个请求并产出到 OutputBus。

        1. 标记 in-flight（防止 TTL 驱逐运行中的 session）
        2. 获取 per-session 锁（同 session 请求串行执行，防止 context 竞争）
        3. 从 CorePool 获取对应 session 的 AgentCore
        4. 调用 agent.prepare_turn() 执行前置处理（含 memory recall）
        5. 调用 AgentKernel.run() 驱动 AgentCore
        6. 通过 OutputBus 广播结果
        """
        session_id = request.session_id
        if session_id in self._cancelled_sessions:
            self._cancelled_sessions.discard(session_id)
            logger.info(
                "KernelScheduler: skipping cancelled session request session_id=%s request_id=%s",
                session_id,
                request.request_id[:8],
            )
            await self._out_bus.publish_error(
                request.request_id,
                asyncio.CancelledError("session cancelled before dispatch"),
            )
            return

        self._inflight_sessions[session_id] += 1
        try:
            # 同 session 的并发请求在此排队，确保 context/turn_id/DB 写入不竞争
            session_lock = await self._get_session_lock(session_id)
        except BaseException:
            pending = self._inflight_sessions.get(session_id, 0) - 1
            if pending > 0:
                self._inflight_sessions[session_id] = pending
            else:
                self._inflight_sessions.pop(session_id, None)
            raise
        async with session_lock:
            try:
                # 准备钩子
                hooks = None
                if self._hooks_factory:
                    hooks = self._hooks_factory(request)
                elif isinstance(request.metadata, dict):
                    # 允许调用方通过 request.metadata["_hooks"] 直接透传运行时回调
                    raw_hooks = request.metadata.get("_hooks")
                    if isinstance(raw_hooks, AgentHooks):
                        hooks = raw_hooks

                # 获取 AgentCore（懒加载）
                # 记忆路径：profile 有非空 frontend_id/dialog_window_id 时优先使用
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

                # Core 级生命周期日志接入（在 prepare_turn 之前注入，确保用户消息被记录）
                entry = self._core_pool.get_entry(session_id)
                core_logger = getattr(entry, "logger", None) if entry is not None else None
                if core_logger is not None and agent._session_logger is None:
                    agent._session_logger = core_logger  # type: ignore[assignment]

                content_items = request.metadata.get("content_items")
                if content_items:
                    logger.info(
                        "scheduler: injecting %d content_items into LLM context for session=%s (types=%s)",
                        len(content_items),
                        session_id,
                        [str(i.get("type")) for i in content_items[:3]],
                    )

                # 前置处理：同步外部更新、memory recall、写入用户消息
                # （统一路径，修复了之前 scheduler 缺失 memory recall 的问题）
                turn_id, summary_task, summary_recent_start = await agent.prepare_turn(
                    request.text, content_items
                )

                # Core 级生命周期日志：记录本轮输入（在 prepare_turn 之后可获得 turn_id）
                if core_logger is not None:
                    try:
                        core_logger.on_turn_start(turn_id, request.text)
                    except Exception:
                        pass

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

                # 统一发布到 out 总线：submit 等待者与订阅者都会收到
                await self._out_bus.publish(session_id, request.request_id, run_result)
                # inject_turn 仍写入 push 队列，供 poll_push 兼容读取
                if not isinstance(request.metadata, dict) or not request.metadata.get("_submit"):
                    self._push_to_queue(session_id, request.request_id, run_result)

            except asyncio.CancelledError:
                # CancelledError 继承自 BaseException（Python 3.8+），不被 except Exception 捕获。
                # 必须显式处理，否则等待该 request_id 的调用方会永久悬挂。
                await self._out_bus.publish_error(
                    request.request_id,
                    asyncio.CancelledError("kernel task cancelled"),
                )
                # inject_turn 被取消时不推送到 out 队列（任务未完成）
                raise
            except Exception as exc:
                logger.exception(
                    "KernelScheduler: error processing request_id=%s: %s",
                    request.request_id[:8],
                    exc,
                )
                err_result = AgentRunResult(
                    output_text=f"[后台任务处理出错] {exc}",
                    metadata={"_push_error": str(exc)},
                )
                self._push_to_queue(session_id, request.request_id, err_result)
                await self._out_bus.publish_error(request.request_id, exc)
            finally:
                pending = self._inflight_sessions.get(session_id, 0) - 1
                if pending > 0:
                    self._inflight_sessions[session_id] = pending
                else:
                    self._inflight_sessions.pop(session_id, None)

    def _push_to_queue(
        self, session_id: str, request_id: str, result: AgentRunResult
    ) -> None:
        """将结果推入 per-session [out] 队列（统一出口，submit 与 inject_turn 均经此路径）。"""
        if session_id not in self._push_queues:
            self._push_queues[session_id] = asyncio.Queue(maxsize=50)
        out_len = len(result.output_text or "")
        envelope = (request_id, result)
        try:
            self._push_queues[session_id].put_nowait(envelope)
            logger.debug(
                "KernelScheduler: out_queue put session_id=%s request_id=%s output_len=%s queue_size=%s",
                session_id,
                request_id[:8] if request_id else "",
                out_len,
                self._push_queues[session_id].qsize(),
                extra={"session_id": session_id},
            )
        except asyncio.QueueFull:
            logger.warning(
                "KernelScheduler: out_queue full session_id=%s output_len=%s dropping result",
                session_id,
                out_len,
                extra={"session_id": session_id},
            )

    def poll_push(
        self, session_id: str
    ) -> Optional[tuple[str, AgentRunResult]]:
        """非阻塞：弹出该 session 的下一条 [out] 队列结果，无则返回 None。

        统一出口：submit 与 inject_turn 的结果均经此队列。
        返回 (request_id, result)，供前端按 session 顺序消费。
        可循环调用至返回 None 以批量取出。
        """
        queue = self._push_queues.get(session_id)
        if queue is None or queue.empty():
            return None
        try:
            envelope = queue.get_nowait()
            request_id, result = envelope
            logger.debug(
                "KernelScheduler: poll_push delivered session_id=%s request_id=%s output_len=%s remaining=%s",
                session_id,
                (request_id or "")[:8],
                len(result.output_text or ""),
                queue.qsize(),
                extra={"session_id": session_id},
            )
            return (request_id, result)
        except asyncio.QueueEmpty:
            return None

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def active_task_count(self) -> int:
        return len(self._active_tasks)

    @property
    def core_pool(self) -> "CorePool":
        """暴露 CorePool 供网关读取内核态 session 状态。"""
        return self._core_pool
