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
from pathlib import Path

from schedule_agent.automation.agent_task import TaskStatus
from schedule_agent.automation.repositories import (
    JobDefinitionRepository,
    JobRunRepository,
)
from schedule_agent.automation.scheduler import AutomationScheduler
from schedule_agent.automation.session_manager import SessionManager
from schedule_agent.automation.task_queue import AgentTaskQueue
from schedule_agent.automation.logging_utils import AutomationTaskLogger
from schedule_agent.config import find_config_file, get_config, load_config

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "agent_worker.log"

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.WARNING)
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.INFO)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[console_handler, file_handler],
)
logger = logging.getLogger("agent_worker")

POLL_INTERVAL_SECONDS = 5
CONFIG_SYNC_INTERVAL_SECONDS = 60.0  # 与 scheduler 的 _reload_interval 对齐


async def _watch_config_and_sync_jobs(job_def_repo: JobDefinitionRepository, config_path) -> None:
    """周期性从 config.yaml 重载 automation.jobs 并同步到 job_definitions.json。"""
    while True:
        await asyncio.sleep(CONFIG_SYNC_INTERVAL_SECONDS)
        try:
            config = load_config(config_path)
            _sync_jobs_from_config(config, job_def_repo)
        except Exception as exc:
            logger.debug("Config sync skipped: %s", exc)


def _sync_jobs_from_config(config, job_def_repo: JobDefinitionRepository) -> None:
    """根据 config.automation.jobs 确保对应 JobDefinition 存在/更新。

    设计原则：
    - 只新增或更新由配置声明的任务，不删除任何已有任务（包括默认内置任务和历史任务）。
    - 通过 (job_type, user_id, name) 三元组匹配已有记录，若存在则更新 interval / enabled / 调度相关 payload。
    """
    automation_cfg = getattr(config, "automation", None)
    if not automation_cfg or not getattr(automation_cfg, "jobs", None):
        return

    existing = job_def_repo.get_all()

    for job_cfg in automation_cfg.jobs:
        times = getattr(job_cfg, "times", None) or []
        has_times = bool(times)
        has_daily_time = bool(getattr(job_cfg, "daily_time", None))

        # times / daily_time 模式：每天固定时刻执行一次，interval_minutes 可以省略。
        if has_times or has_daily_time:
            interval_seconds = 24 * 3600
        else:
            # 纯 interval 或 start_time + interval 模式：需要合法的 interval_minutes
            minutes = getattr(job_cfg, "interval_minutes", None)
            try:
                interval_seconds = int(minutes) * 60
            except Exception:
                # 缺少或非法的 interval 配置时，跳过该任务，避免影响其他任务。
                continue

            if interval_seconds <= 0:
                continue

        target_job_type = job_cfg.job_type
        target_user_id = job_cfg.user_id
        target_name = getattr(job_cfg, "name", "").strip()
        target_instruction = job_cfg.description

        matched = None
        for job in existing:
            payload = job.payload_template or {}
            # 优先按 (job_type, user_id, name) 三元组匹配
            if target_name:
                if (
                    job.job_type == target_job_type
                    and str(payload.get("user_id", "default")) == target_user_id
                    and str(payload.get("name", "")) == target_name
                ):
                    matched = job
                    break
            # 回退兼容：旧数据没有 name 时，使用 description 作为匹配键，并在命中后写回 name
            else:
                if (
                    job.job_type == target_job_type
                    and str(payload.get("user_id", "default")) == target_user_id
                    and str(payload.get("instruction", "")) == target_instruction
                ):
                    matched = job
                    break

        if matched is not None:
            changed = False
            if matched.interval_seconds != interval_seconds:
                matched.interval_seconds = interval_seconds
                changed = True
            # 同步 enabled 状态
            if matched.enabled != job_cfg.enabled:
                matched.enabled = job_cfg.enabled
                changed = True
            # 同步 payload 配置（daily_time / times / start_time 等）
            payload = matched.payload_template or {}
            # 写回 name，保证后续可以稳定通过 name 识别
            if target_name and payload.get("name") != target_name:
                payload["name"] = target_name
                changed = True
            if has_daily_time and payload.get("daily_time") != job_cfg.daily_time:
                payload["daily_time"] = job_cfg.daily_time
                changed = True
            if has_times and payload.get("times") != times:
                payload["times"] = list(times)
                changed = True
            if getattr(job_cfg, "start_time", None) and payload.get("start_time") != job_cfg.start_time:
                payload["start_time"] = job_cfg.start_time
                changed = True
            if changed:
                matched.payload_template = payload
            if changed:
                job_def_repo.update(matched)
        else:
            from schedule_agent.automation.types import JobDefinition

            job = JobDefinition(
                job_type=target_job_type,
                enabled=job_cfg.enabled,
                interval_seconds=interval_seconds,
                timezone=config.time.timezone,
                payload_template={
                    "name": target_name or target_instruction,
                    "instruction": target_instruction,
                    "user_id": target_user_id,
                    **(
                        {"daily_time": job_cfg.daily_time}
                        if has_daily_time
                        else {}
                    ),
                    **(
                        {"times": list(times)}
                        if has_times
                        else {}
                    ),
                    **(
                        {"start_time": job_cfg.start_time}
                        if getattr(job_cfg, "start_time", None)
                        else {}
                    ),
                },
            )
            job_def_repo.create(job)


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

    job_def_repo = JobDefinitionRepository()
    # 将 config.automation.jobs 中声明的任务同步到 JobDefinitionRepository。
    # 不再自动补充任何「默认」任务，所有调度均由配置或 Agent 工具创建。
    _sync_jobs_from_config(config, job_def_repo)

    config_path = find_config_file()
    config_watch_task = asyncio.create_task(
        _watch_config_and_sync_jobs(job_def_repo, config_path),
        name="config_sync",
    )

    scheduler = AutomationScheduler(job_def_repo=job_def_repo, job_run_repo=JobRunRepository(), task_queue=queue)
    await scheduler.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("Agent worker started. Polling every %ds.", POLL_INTERVAL_SECONDS)

    try:
        await _consume_loop(queue, session_manager, stop_event)
    finally:
        config_watch_task.cancel()
        try:
            await config_watch_task
        except asyncio.CancelledError:
            pass
        await scheduler.stop()
        await session_manager.close_all()
        logger.info("Agent worker stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        sys.exit(0)
