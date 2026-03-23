"""Background scheduler service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .event_bus import AsyncEventBus
from .repositories import JobDefinitionRepository, JobRunRepository
from .types import JobDefinition, JobRun, JobStatus

if TYPE_CHECKING:
    from .task_queue import AgentTaskQueue


class AutomationScheduler:
    def __init__(
        self,
        event_bus: Optional[AsyncEventBus] = None,
        job_def_repo: Optional[JobDefinitionRepository] = None,
        job_run_repo: Optional[JobRunRepository] = None,
        task_queue: Optional["AgentTaskQueue"] = None,
    ):
        self._event_bus = event_bus
        self._job_def_repo = job_def_repo or JobDefinitionRepository()
        self._job_run_repo = job_run_repo or JobRunRepository()
        self._task_queue = task_queue
        # job_name -> 调度协程任务
        self._tasks: dict[str, asyncio.Task] = {}
        # job_name -> 最近一次用于调度的 JobDefinition 快照（用于检测配置变更）
        self._job_snapshots: dict[str, JobDefinition] = {}
        self._running = False
        # 周期性检查 job_definitions 是否有新增/禁用的任务，默认 60s 刷新一次。
        self._reload_interval: float = 60.0
        self._watch_task: Optional[asyncio.Task] = None

    def tracked_jobs_snapshot(self) -> Dict[str, Any]:
        """供 KernelTerminal / 运维查看：当前进程内为每个 job 维护的调度协程状态。

        返回 JSON 友好结构：scheduler 是否在跑、watcher 状态、每个 job 的定义摘要与 asyncio.Task 是否存活。
        """
        jobs: List[Dict[str, Any]] = []
        for job_name, task in sorted(self._tasks.items(), key=lambda x: x[0]):
            row: Dict[str, Any] = {
                "job_name": job_name,
                "asyncio_task_name": task.get_name(),
                "task_done": task.done(),
                "task_cancelled": task.cancelled(),
            }
            snap = self._job_snapshots.get(job_name)
            if snap is not None:
                pt = snap.payload_template or {}
                row["definition"] = {
                    "job_type": snap.job_type,
                    "name": pt.get("name", ""),
                    "enabled": snap.enabled,
                    "one_shot": snap.one_shot,
                    "interval_seconds": snap.interval_seconds,
                    "timezone": snap.timezone or "Asia/Shanghai",
                    "run_at": snap.run_at.isoformat() if snap.run_at else None,
                }
            if task.done() and not task.cancelled():
                try:
                    exc = task.exception()
                    if exc is not None:
                        row["task_error"] = repr(exc)
                except (asyncio.InvalidStateError, Exception):
                    pass
            jobs.append(row)

        watcher: Optional[Dict[str, Any]] = None
        wt = self._watch_task
        if wt is not None:
            watcher = {
                "name": wt.get_name(),
                "done": wt.done(),
                "cancelled": wt.cancelled(),
            }

        return {
            "scheduler_running": self._running,
            "reload_interval_seconds": self._reload_interval,
            "tracked_job_count": len(jobs),
            "jobs": jobs,
            "watcher": watcher,
        }

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        for job in self._job_def_repo.get_enabled():
            self._tasks[job.job_name] = asyncio.create_task(
                self._run_loop(job),
                name=f"scheduler:{job.job_name}",
            )
            # 记录一份快照，后续用于检测配置是否发生变化
            try:
                self._job_snapshots[job.job_name] = job.model_copy(deep=True)
            except Exception:  # pragma: no cover - 理论上不会触发
                self._job_snapshots[job.job_name] = job

        # 队列驱动模式下，支持在运行期通过修改 job_definitions.json 添加/禁用任务，
        # 由后台 watcher 周期性刷新内存中的任务列表，避免必须重启后台守护进程（如 automation_daemon）。
        self._watch_task = asyncio.create_task(
            self._watch_job_definitions(), name="scheduler:watcher"
        )

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._job_snapshots.clear()

        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watch_task = None

    async def run_job_once(self, job: JobDefinition) -> JobRun:
        run = JobRun(
            job_name=job.job_name,
            job_type=job.job_type,
            triggered_at=datetime.now(),
            started_at=datetime.now(),
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
            run.finished_at = datetime.now()
            self._job_run_repo.update(run)
            if job.one_shot and job.enabled:
                job.enabled = False
                job.updated_at = datetime.now()
                self._job_def_repo.update(job)
        return run

    async def _run_loop(self, job: JobDefinition) -> None:
        """持续调度单个 Job。

        注意：首次执行前会先等待到下一次计划触发时间，
        避免在 scheduler / 后台守护进程启动瞬间就立刻跑一遍。
        """
        while self._running:
            delay = self._compute_sleep_seconds(job)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            await self.run_job_once(job)
            if job.one_shot:
                break

    def _compute_sleep_seconds(self, job: JobDefinition) -> float:
        """根据 JobDefinition 计算下一次调度前应 sleep 的秒数。

        优先顺序：
        0. one_shot + run_at: 指定时间触发一次
        1. payload['times']: 每天多个闹钟时间点（HH:MM 列表）
        2. payload['start_time'] + job.interval_seconds: 起始时刻 + 间隔
        3. payload['daily_time']: 每天单个闹钟时间点
        4. 退回简单的 interval_seconds 间隔
        """
        delay = float(job.interval_seconds)
        payload = job.payload_template or {}

        # 使用 job 指定的时区，脱离全局 TZ 环境变量
        try:
            tz = ZoneInfo(job.timezone or "Asia/Shanghai")
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")

        # 0) 一次性闹钟：one_shot + run_at
        if job.one_shot and job.run_at is not None:
            if job.run_at.tzinfo is None:
                target = job.run_at.replace(tzinfo=tz)
            else:
                target = job.run_at.astimezone(tz)
            now = datetime.now(tz)
            return max(0.0, (target - now).total_seconds())

        # 1) 多个闹钟时间点：payload['times'] = ["08:00", "14:00", ...]
        times_raw = payload.get("times")
        times_list: list[str] = []
        if isinstance(times_raw, list):
            times_list = [str(t).strip() for t in times_raw if str(t).strip()]
        elif isinstance(times_raw, str):
            times_list = [s.strip() for s in times_raw.split(",") if s.strip()]
        if times_list:
            candidates: list[int] = []
            for t in times_list:
                try:
                    h_str, m_str = t.split(":", 1)
                    h = int(h_str)
                    m = int(m_str)
                    if 0 <= h <= 23 and 0 <= m <= 59:
                        candidates.append(h * 3600 + m * 60)
                except Exception:
                    continue
            if candidates:
                now = datetime.now(tz)
                now_sec = now.hour * 3600 + now.minute * 60 + now.second
                today_future = [c for c in candidates if c > now_sec]
                if today_future:
                    next_sec = min(today_future)
                    seconds = next_sec - now_sec
                else:
                    # 今天都过了，选明天的最早一个
                    first = min(candidates)
                    seconds = (24 * 3600 - now_sec) + first
                return max(1.0, float(seconds))

        # 2) 起始时刻 + 间隔：payload['start_time'] + job.interval_seconds
        start_time = payload.get("start_time")
        if start_time:
            try:
                h_str, m_str = str(start_time).split(":", 1)
                h = int(h_str)
                m = int(m_str)
                if 0 <= h <= 23 and 0 <= m <= 59:
                    period = max(1.0, delay)
                    now = datetime.now(tz)
                    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    first = midnight + timedelta(hours=h, minutes=m)
                    t = first
                    # 沿着时间线按 interval 向前推进，直到超过当前时间
                    while t <= now:
                        t = t + timedelta(seconds=period)
                    seconds = (t - now).total_seconds()
                    return max(1.0, seconds)
            except Exception:
                # 配置非法时回退其他语义
                pass

        # 3) 单个 daily_time 闹钟
        daily_time = payload.get("daily_time")
        if daily_time:
            try:
                # daily_time: "HH:MM"
                hour_str, minute_str = str(daily_time).split(":", 1)
                hour = int(hour_str)
                minute = int(minute_str)
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    return max(1.0, delay)
            except Exception:
                # 配置非法时回退 interval 语义
                return max(1.0, delay)

            now = datetime.now(tz)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)

            seconds = (target - now).total_seconds()
            return max(1.0, seconds)

        # 4) 退回简单 interval 语义
        return max(1.0, delay)

    def _job_changed(self, old: JobDefinition, new: JobDefinition) -> bool:
        """检测 JobDefinition 是否发生了会影响调度的配置变更。"""
        if old.enabled != new.enabled:
            return True
        if old.interval_seconds != new.interval_seconds:
            return True
        if old.one_shot != new.one_shot:
            return True
        if old.run_at != new.run_at:
            return True
        if (old.timezone or "") != (new.timezone or ""):
            return True
        if (old.payload_template or {}) != (new.payload_template or {}):
            return True
        if old.job_type != new.job_type:
            return True
        return False

    async def _watch_job_definitions(self) -> None:
        """后台刷新 job_definitions，支持运行时新增/禁用定时任务."""
        while self._running:
            await asyncio.sleep(self._reload_interval)
            try:
                # 尝试从 config.automation.jobs 同步最新配置到 job_definitions.json，
                # 这样在不重启 daemon 的情况下，修改 config.yaml 也能在约 60 秒内生效。
                try:
                    from .config_sync import sync_job_definitions_from_config

                    sync_job_definitions_from_config(
                        config=None, job_def_repo=self._job_def_repo
                    )
                except Exception:
                    # 同步失败不应影响已有调度逻辑。
                    pass

                enabled_jobs = self._job_def_repo.get_enabled()
                enabled_ids = {job.job_name for job in enabled_jobs}

                # 新增或配置发生变化的 enabled 任务：启动/重启对应调度协程
                for job in enabled_jobs:
                    existing_task = self._tasks.get(job.job_name)
                    snapshot = self._job_snapshots.get(job.job_name)

                    # 1) 全新任务：还没有调度协程
                    if existing_task is None:
                        self._tasks[job.job_name] = asyncio.create_task(
                            self._run_loop(job),
                            name=f"scheduler:{job.job_name}",
                        )
                        try:
                            self._job_snapshots[job.job_name] = job.model_copy(deep=True)
                        except Exception:  # pragma: no cover
                            self._job_snapshots[job.job_name] = job
                        continue

                    # 2) 已有任务，但配置发生了变化（例如 daily_time 从 13:45 改为 14:00）
                    if snapshot is not None and self._job_changed(snapshot, job):
                        existing_task.cancel()
                        self._tasks[job.job_name] = asyncio.create_task(
                            self._run_loop(job),
                            name=f"scheduler:{job.job_name}",
                        )
                        try:
                            self._job_snapshots[job.job_name] = job.model_copy(deep=True)
                        except Exception:  # pragma: no cover
                            self._job_snapshots[job.job_name] = job

                # 已被删除或禁用的任务：取消并移除调度协程
                for job_name in list(self._tasks.keys()):
                    if job_name not in enabled_ids:
                        task = self._tasks.pop(job_name)
                        task.cancel()
                        self._job_snapshots.pop(job_name, None)
            except Exception:
                # 防御性：不让 watcher 异常影响主循环。
                continue

    async def _dispatch_job(self, job: JobDefinition) -> None:
        if self._task_queue is None:
            return
        await self._dispatch_via_queue(job)

    async def _dispatch_via_queue(self, job: JobDefinition) -> None:
        """构造 AgentTask 并推送到队列，session_id 含日期保证当天唯一。"""
        from .agent_task import make_cron_task

        payload = job.payload_template or {}
        instruction = payload.get("instruction")
        if not instruction:
            instruction = (
                f"这是自动化定时任务（{job.job_name}）。"
                "请根据任务配置执行适当的操作，并仅输出“操作 + 结果”。"
            )

        user_id = str(payload.get("user_id") or "default")
        memory_owner = str(payload.get("memory_owner") or "").strip()
        core_mode = str(payload.get("core_mode") or "").strip() or None

        task = make_cron_task(
            job.job_name,
            instruction=instruction,
            user_id=user_id,
            memory_owner=memory_owner or None,
            core_mode=core_mode,
        )
        self._task_queue.push(task)  # type: ignore[union-attr]
