"""
KernelScheduler — 单线程异步调度器。

类比 OS 进程调度器，InputQueue + dispatch_loop + create_task 实现跨 session 真并发

设计原则：
- asyncio.PriorityQueue 按 (priority, enqueued_at) 排序，高优先级先处理，同优先级 FIFO
- _dispatch_loop 使用 create_task，不 await 任务本身，让多个 session 的 IO 真正并发（协作式）
- submit() 语义收敛为「仅提交到 [in] 队列」，返回 request_id
- 所有结果统一经 OutputBus.publish() 广播；需要同步等待的一方按 request_id await
- 输出单出口：Scheduler 不持有 push_queues，消费方通过 subscribe_out() 自行缓冲
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Union

from agent_core.interfaces import AgentHooks, AgentRunResult
from agent_core.kernel_interface import KernelRequest

from .output_bus import OutputBus
if TYPE_CHECKING:
    from .kernel import AgentKernel
    from .core_pool import CorePool

logger = logging.getLogger(__name__)

_IN_QUEUE_WARN_THRESHOLD = 500


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

    输出模型：
    - 所有结果统一经 OutputBus.publish() 一个出口
    - submit 调用方通过 Future 精准获取结果
    - 其余消费方通过 subscribe_out() 注册 listener，自行决定缓冲/推送策略

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
        set_scheduler = getattr(self._core_pool, "set_scheduler", None)
        if callable(set_scheduler):
            set_scheduler(self)
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
        # session_id -> 当前正在运行的 _run_and_route Tasks（用于 cancel_session_tasks）
        self._session_active_tasks: Dict[str, set[asyncio.Task]] = defaultdict(set)
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
        - 完成后通过 OutputBus.publish() 广播给所有订阅者

        典型用途：
        - CorePool.on_sub_complete/on_sub_fail 唤醒父 session（first-done 语义）
        - SendMessageToAgentTool / ReplyToMessageTool 的 P2P 消息投递
        """
        qsize = self._queue.qsize()
        if qsize >= _IN_QUEUE_WARN_THRESHOLD:
            logger.warning(
                "KernelScheduler: input queue size %d exceeds threshold %d, "
                "inject_turn may cause backpressure",
                qsize,
                _IN_QUEUE_WARN_THRESHOLD,
            )
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
        """取消指定 session 的所有活跃执行任务并标记为已取消。

        同时处理两种情况：
        - 请求已在执行 (_session_active_tasks) -> 直接 cancel 所有 Tasks
        - 请求仍在队列中等待 dispatch -> 加入 _cancelled_sessions，
          _run_and_route 开头会检查并跳过
        """
        self._cancelled_sessions.add(session_id)
        tasks = self._session_active_tasks.get(session_id, set())
        cancelled_any = False
        for task in list(tasks):
            if not task.done():
                task.cancel()
                cancelled_any = True
        if cancelled_any:
            logger.info(
                "KernelScheduler: cancelled %d active task(s) for session_id=%s",
                len(tasks),
                session_id,
            )
        else:
            logger.info(
                "KernelScheduler: marked session_id=%s as cancelled (no active task)",
                session_id,
            )
        return cancelled_any

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
            self._session_active_tasks[request.session_id].add(task)

            def _cleanup(t: asyncio.Task, sid: str = request.session_id) -> None:
                self._active_tasks.discard(t)
                task_set = self._session_active_tasks.get(sid)
                if task_set is not None:
                    task_set.discard(t)
                    if not task_set:
                        self._session_active_tasks.pop(sid, None)

            task.add_done_callback(_cleanup)
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
        5. 调用 AgentKernel.run() 驱动 AgentCore（上下文压缩由 run_loop 信号触发）
        6. 通过 OutputBus.publish() 广播结果（唯一出口）
        """
        session_id = request.session_id
        if session_id in self._cancelled_sessions:
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
            agent = None
            turn_id = 0
            run_result = None
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
                # 记忆路径：优先 metadata.memory_owner（feishu:uid），与 job 配置一致；
                # 否则用 CoreProfile.frontend_id/dialog_window_id；再否则 metadata.source/user_id。
                md = request.metadata if isinstance(request.metadata, dict) else {}
                mo_raw = str(md.get("memory_owner") or "").strip()
                profile = request.profile
                if mo_raw and ":" in mo_raw:
                    mem_source, mem_user_id = mo_raw.split(":", 1)
                    mem_source, mem_user_id = mem_source.strip(), mem_user_id.strip()
                elif profile and (profile.frontend_id or "").strip():
                    mem_source = profile.frontend_id.strip()
                    mem_user_id = (profile.dialog_window_id or "").strip() or "root"
                else:
                    mem_source = str(md.get("source") or request.frontend_id or "cli")
                    mem_user_id = str(md.get("user_id") or "root")

                agent = await self._core_pool.acquire(
                    request.session_id,
                    source=mem_source,
                    user_id=mem_user_id,
                    profile=profile,
                )
                # 供工具层解析 MEMORY.md / 与任务 owner 对齐（即使 agent._source 曾为 cron:job）
                setattr(agent, "_job_memory_owner", mo_raw or None)

                # Core 级生命周期日志接入（在 prepare_turn 之前注入，确保用户消息被记录）
                entry = self._core_pool.get_live_entry(session_id)
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
                turn_id = await agent.prepare_turn(request.text, content_items)

                # Core 级生命周期日志：记录本轮输入（在 prepare_turn 之后可获得 turn_id）
                if core_logger is not None:
                    try:
                        core_logger.on_turn_start(
                            turn_id, request.text, request_id=request.request_id
                        )
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
                await agent._finalize_turn(run_result)

                # Core 级生命周期日志：记录本轮输出（正常路径）
                if core_logger is not None:
                    try:
                        core_logger.on_turn_end(
                            turn_id,
                            output_text=run_result.output_text,
                            metadata=run_result.metadata,
                            request_id=request.request_id,
                        )
                    except Exception:
                        pass

                # 刷新 TTL（每次请求完成后更新活跃时间）
                self._core_pool.touch(session_id)

                # 统一发布到 OutputBus（唯一出口）：
                # - submit 等待者通过 Future 获取结果
                # - 所有 subscriber 通过 listener 回调获取结果
                await self._out_bus.publish(session_id, request.request_id, run_result)

            except asyncio.CancelledError:
                # 取消时也写 checkpoint，保证 daemon 重启后可恢复
                if agent is not None:
                    try:
                        await agent._finalize_turn(None)
                    except Exception:
                        pass
                # 异常/取消路径：记录 turn_end，保证每轮都有结束记录
                entry = self._core_pool.get_live_entry(session_id)
                core_logger = getattr(entry, "logger", None) if entry is not None else None
                if core_logger is not None:
                    try:
                        core_logger.on_turn_end(
                            turn_id,
                            output_text="",
                            metadata={"cancelled": True, "reason": "kernel task cancelled"},
                            request_id=request.request_id,
                        )
                    except Exception:
                        pass
                await self._out_bus.publish_error(
                    request.request_id,
                    asyncio.CancelledError("kernel task cancelled"),
                )
                raise
            except Exception as exc:
                # 异常时也写 checkpoint，保证 daemon 重启后可从未完成状态恢复
                if agent is not None:
                    try:
                        await agent._finalize_turn(None)
                    except Exception:
                        pass
                # 异常路径：记录 turn_end，保证每轮都有结束记录
                entry = self._core_pool.get_live_entry(session_id)
                core_logger = getattr(entry, "logger", None) if entry is not None else None
                if core_logger is not None:
                    try:
                        err_output = f"[后台任务处理出错] {exc}"
                        core_logger.on_turn_end(
                            turn_id,
                            output_text=err_output,
                            metadata={"error": str(exc), "error_type": type(exc).__name__},
                            request_id=request.request_id,
                        )
                    except Exception:
                        pass
                logger.exception(
                    "KernelScheduler: error processing request_id=%s: %s",
                    request.request_id[:8],
                    exc,
                )
                # 区分两种错误投递路径：
                # - 有 waiter（submit 路径）→ publish_error 以异常形式交给 await 方
                # - 无 waiter（inject_turn 路径）→ publish 包装后的 AgentRunResult 给订阅者
                if self._out_bus.has_waiter(request.request_id):
                    await self._out_bus.publish_error(request.request_id, exc)
                else:
                    err_result = AgentRunResult(
                        output_text=f"[后台任务处理出错] {exc}",
                        metadata={"_push_error": str(exc)},
                    )
                    await self._out_bus.publish(session_id, request.request_id, err_result)
            finally:
                pending = self._inflight_sessions.get(session_id, 0) - 1
                if pending > 0:
                    self._inflight_sessions[session_id] = pending
                else:
                    self._inflight_sessions.pop(session_id, None)

    def clear_cancelled(self, session_id: str) -> None:
        """清除指定 session 的取消标记（当该 session 再次变为活跃时调用）。"""
        self._cancelled_sessions.discard(session_id)

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
