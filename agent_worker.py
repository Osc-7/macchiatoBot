#!/usr/bin/env python3
"""Queue-driven Agent worker entrypoint.

Starts:
  1. AutomationScheduler (push mode) — periodically pushes AgentTask to the queue.
  2. TaskConsumer loop — polls the queue and runs each task through ScheduleAgent.

This is the new recommended background process. The old automation_worker.py
(direct service execution) is kept for compatibility during the transition period.

Usage::

    python agent_worker.py
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from schedule_agent.automation.agent_task import TaskStatus
from schedule_agent.automation.repositories import (
    JobDefinitionRepository,
    JobRunRepository,
)
from schedule_agent.automation.scheduler import AutomationScheduler
from schedule_agent.automation.session_manager import SessionManager
from schedule_agent.automation.task_queue import AgentTaskQueue
from schedule_agent.automation.logging_utils import AutomationTaskLogger
from schedule_agent.config import get_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("agent_worker")

POLL_INTERVAL_SECONDS = 5


async def _consume_loop(
    queue: AgentTaskQueue,
    session_manager: SessionManager,
    stop_event: asyncio.Event,
) -> None:
    """持续从队列取任务并交由 SessionManager / Agent 执行。"""
    while not stop_event.is_set():
        task = queue.pop_pending()
        if task is None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(stop_event.wait()),
                    timeout=POLL_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                pass
            continue

        task_logger = AutomationTaskLogger(task)
        task_logger.log_task_start()

        logger.info(
            "Running task %s | source=%s | session=%s | policy=%s",
            task.task_id,
            task.source,
            task.session_id,
            task.context_policy,
        )
        try:

            async def on_trace_event(event: dict) -> None:
                task_logger.log_trace_event(event)

            result = await session_manager.run_task(
                session_id=task.session_id,
                instruction=task.instruction,
                context_policy=task.context_policy,
                on_trace_event=on_trace_event,
            )
            op_ok, op_problems = task_logger.evaluate_required_operations()
            if op_ok:
                queue.update_status(task.task_id, TaskStatus.SUCCESS, result=result)
                task_logger.log_task_end(status=TaskStatus.SUCCESS, result=result, error=None)
                logger.info("Task %s succeeded", task.task_id)
            else:
                error_msg = "; ".join(op_problems)
                queue.update_status(task.task_id, TaskStatus.FAILED, result=result, error=error_msg)
                task_logger.log_task_end(status=TaskStatus.FAILED, result=result, error=error_msg)
                logger.warning("Task %s marked failed: %s", task.task_id, error_msg)
        except Exception as exc:
            logger.exception("Task %s failed: %s", task.task_id, exc)
            task_logger.log_task_end(status=TaskStatus.FAILED, result=None, error=str(exc))
            queue.update_status(task.task_id, TaskStatus.FAILED, error=str(exc))


async def _main() -> None:
    config = get_config()

    # 导入工具工厂（复用 main.py 中已定义好的工具列表逻辑）
    try:
        from main import get_default_tools  # type: ignore[import]
    except ImportError:
        logger.warning(
            "Could not import get_default_tools from main.py; "
            "agent will run with no tools registered."
        )
        get_default_tools = lambda cfg=None: []  # noqa: E731

    queue = AgentTaskQueue()

    # 恢复上次崩溃未完成的任务
    recovered = queue.recover_stale_running()
    if recovered:
        logger.info("Recovered %d stale running tasks as pending", recovered)

    session_manager = SessionManager(
        config=config,
        tools_factory=lambda: get_default_tools(config),
    )

    scheduler = AutomationScheduler(
        job_def_repo=JobDefinitionRepository(),
        job_run_repo=JobRunRepository(),
        task_queue=queue,
    )
    scheduler.ensure_default_jobs()
    await scheduler.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("Agent worker started. Polling every %ds.", POLL_INTERVAL_SECONDS)

    try:
        await _consume_loop(queue, session_manager, stop_event)
    finally:
        await scheduler.stop()
        await session_manager.close_all()
        logger.info("Agent worker stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        sys.exit(0)
