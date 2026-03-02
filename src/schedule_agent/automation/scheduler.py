"""Background scheduler service."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Dict, Optional

from .event_bus import AsyncEventBus
from .repositories import JobDefinitionRepository, JobRunRepository
from .types import JobDefinition, JobRun, JobStatus

if TYPE_CHECKING:
    from .task_queue import AgentTaskQueue

# job_type → 给 Agent 的自然语言指令模板
_JOB_INSTRUCTIONS: Dict[str, str] = {
    "sync.course": (
        "这是自动化定时任务。请只执行以下操作："
        "调用 sync_canvas(days_ahead=30, write_tasks=true, write_deadline_events=true)。"
        "然后仅输出“操作 + 结果”，不要提出追问或建议。"
    ),
    "sync.email": (
        "这是自动化定时任务。请只执行以下操作："
        "调用 sync_sources(source='email')。"
        "然后仅输出“操作 + 结果”，不要提出追问或建议。"
    ),
    "summary.daily": (
        "这是自动化定时任务。请只执行以下操作："
        "调用 get_digest(digest_type='daily', generate_if_missing=true)。"
        "然后仅输出“操作 + 结果”，不要提出追问或建议。"
    ),
    "summary.weekly": (
        "这是自动化定时任务。请只执行以下操作："
        "调用 get_digest(digest_type='weekly', generate_if_missing=true)。"
        "然后仅输出“操作 + 结果”，不要提出追问或建议。"
    ),
}


class AutomationScheduler:
    def __init__(
        self,
        event_bus: Optional[AsyncEventBus] = None,
        job_def_repo: Optional[JobDefinitionRepository] = None,
        job_run_repo: Optional[JobRunRepository] = None,
        task_queue: Optional["AgentTaskQueue"] = None,
    ):
        """
        Args:
            event_bus:   进程内事件总线，兼容旧路径（task_queue 未设时使用）。
            job_def_repo: 作业定义仓库。
            job_run_repo: 作业运行记录仓库。
            task_queue:  AgentTask 队列。设置后，_dispatch_job 将向队列推送任务
                         而非通过 event_bus 直接触发业务逻辑。
        """
        self._event_bus = event_bus
        self._job_def_repo = job_def_repo or JobDefinitionRepository()
        self._job_run_repo = job_run_repo or JobRunRepository()
        self._task_queue = task_queue
        self._tasks: Dict[str, asyncio.Task] = {}
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        for job in self._job_def_repo.get_enabled():
            self._tasks[job.job_id] = asyncio.create_task(self._run_loop(job), name=f"scheduler:{job.job_id}")

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def run_job_once(self, job: JobDefinition) -> JobRun:
        run = JobRun(
            job_id=job.job_id,
            job_type=job.job_type,
            triggered_at=datetime.utcnow(),
            started_at=datetime.utcnow(),
            status=JobStatus.RUNNING,
        )
        self._job_run_repo.create(run)

        try:
            await self._dispatch_job(job)
            run.status = JobStatus.SUCCESS
            run.metrics = {"trigger": "scheduler"}
            run.error = None
        except Exception as exc:  # pragma: no cover
            run.status = JobStatus.FAILED
            run.error = str(exc)
        finally:
            run.finished_at = datetime.utcnow()
            self._job_run_repo.update(run)
        return run

    async def _run_loop(self, job: JobDefinition) -> None:
        while self._running:
            await self.run_job_once(job)
            await asyncio.sleep(job.interval_seconds)

    async def _dispatch_job(self, job: JobDefinition) -> None:
        # 队列模式：推送 AgentTask，由 agent_worker 消费执行
        if self._task_queue is not None:
            await self._dispatch_via_queue(job)
            return

        # 兼容旧路径：通过 event_bus 直接触发业务逻辑
        if self._event_bus is None:
            return
        payload = dict(job.payload_template or {})
        if job.job_type.startswith("sync."):
            payload.setdefault("source_type", job.job_type.split(".", 1)[1])
            await self._event_bus.publish("sync.requested", payload)
            return
        if job.job_type == "summary.daily":
            payload.setdefault("digest_type", "daily")
            await self._event_bus.publish("summary.requested", payload)
            return
        if job.job_type == "summary.weekly":
            payload.setdefault("digest_type", "weekly")
            await self._event_bus.publish("summary.requested", payload)
            return

    async def _dispatch_via_queue(self, job: JobDefinition) -> None:
        """构造 AgentTask 并推送到队列，session_id 含日期保证当天唯一。"""
        from .agent_task import make_cron_task

        instruction = _JOB_INSTRUCTIONS.get(job.job_type)
        if instruction is None:
            return

        task = make_cron_task(
            job_type=job.job_type,
            instruction=instruction,
            user_id=job.payload_template.get("user_id", "default") if job.payload_template else "default",
        )
        self._task_queue.push(task)  # type: ignore[union-attr]

    def ensure_default_jobs(self) -> None:
        existing = {job.job_type for job in self._job_def_repo.get_all()}
        defaults = [
            JobDefinition(job_type="sync.course", interval_seconds=6 * 3600),
            JobDefinition(job_type="sync.email", interval_seconds=2 * 3600),
            JobDefinition(job_type="summary.daily", interval_seconds=24 * 3600),
            JobDefinition(job_type="summary.weekly", interval_seconds=7 * 24 * 3600),
        ]
        for job in defaults:
            if job.job_type not in existing:
                self._job_def_repo.create(job)
