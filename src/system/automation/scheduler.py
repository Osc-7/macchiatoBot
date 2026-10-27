"""Background scheduler service."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from zoneinfo import ZoneInfo

_logger = logging.getLogger(__name__)

from .calendar import (
    DEFAULT_TIMEZONE,
    describe_schedule,
    effective_spec,
    exhaustion_reason,
    next_fire_at_for_job,
    previous_fire_at,
    remaining_runs,
)
from .event_bus import AsyncEventBus
from .repositories import JobDefinitionRepository, JobRunRepository
from .types import JobDefinition, JobRun, JobStatus

if TYPE_CHECKING:
    from .task_queue import AgentTaskQueue


# 同一计划时刻失败后的进程内重试。放弃该格时不扣配额，下一格照常排。
_SLOT_MAX_ATTEMPTS = 3
_SLOT_RETRY_DELAYS_SECONDS = (1.0, 4.0)


def _run_status(run: JobRun) -> str:
    return str(getattr(run.status, "value", run.status) or "").lower()


def _same_instant(a: datetime, b: datetime) -> bool:
    """比较两个 datetime 是否指向同一时刻（容忍 naive/aware 混用与亚秒差）。"""
    try:
        return abs(a.timestamp() - b.timestamp()) < 1.0
    except Exception:
        return a == b


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
                instruction = pt.get("instruction") or ""
                row["definition"] = {
                    "job_type": snap.job_type,
                    "name": pt.get("name", ""),
                    "enabled": snap.enabled,
                    "one_shot": snap.one_shot,
                    "interval_seconds": snap.interval_seconds,
                    "timezone": snap.timezone or "Asia/Shanghai",
                    "run_at": snap.run_at.isoformat() if snap.run_at else None,
                    "daily_time": pt.get("daily_time"),
                    "times": pt.get("times"),
                    "start_time": pt.get("start_time"),
                    "user_id": pt.get("user_id"),
                    "memory_owner": pt.get("memory_owner"),
                    "core_mode": pt.get("core_mode"),
                    "instruction_preview": instruction[:80] + ("…" if len(instruction) > 80 else ""),
                }
                try:
                    spec = effective_spec(snap)
                    row["schedule"] = spec.to_dict()
                    row["schedule_desc"] = describe_schedule(spec)
                    row["runs_done"] = int(getattr(snap, "runs_done", 0) or 0)
                    row["remaining_runs"] = remaining_runs(snap, spec)
                    tz = ZoneInfo(snap.timezone or DEFAULT_TIMEZONE)
                    nxt = next_fire_at_for_job(snap, datetime.now(tz), tz)
                    row["next_run_at"] = nxt.isoformat() if nxt else None
                except Exception:
                    row["schedule"] = snap.schedule
                    row["schedule_desc"] = "unknown"
                    row["next_run_at"] = None
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

    async def run_job_once(
        self,
        job: JobDefinition,
        scheduled_at: Optional[datetime] = None,
    ) -> JobRun:
        # 幂等防线（持久化层）：同一 job 的同一计划时刻只消费一次。
        # _run_loop 里的 last_fired 只防进程内重放；这里覆盖 watcher 重启 loop、
        # daemon 重启后从 checkpoint 冷启动、以及任何未来可能出现的并发实例。
        if scheduled_at is not None:
            previous = self._find_duplicate_run(job.job_name, scheduled_at)
            if previous is not None:
                _logger.warning(
                    "scheduler: skip duplicate fire job=%s scheduled_at=%s (already %s, run_id=%s)",
                    job.job_name,
                    scheduled_at.isoformat(),
                    previous.status,
                    previous.run_id,
                )
                return previous
        run = JobRun(
            job_name=job.job_name,
            job_type=job.job_type,
            triggered_at=datetime.now(),
            scheduled_at=scheduled_at,
            started_at=datetime.now(),
            status=JobStatus.RUNNING,
        )
        self._job_run_repo.create(run)

        try:
            await self._dispatch_job(job)
            run.status = JobStatus.SUCCESS
            run.metrics = {
                "trigger": "scheduler",
                "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
            }
            run.error = None
        except Exception as exc:  # pragma: no cover
            run.status = JobStatus.FAILED
            run.error = str(exc)
        finally:
            run.finished_at = datetime.now()
            self._job_run_repo.update(run)
            # 失败留给同一时刻重试；只有这次真正跑成才收掉一次性任务。
            if job.one_shot and job.enabled and run.status == JobStatus.SUCCESS:
                job.enabled = False
                job.updated_at = datetime.now()
                self._job_def_repo.update(job)
        return run

    async def _run_loop(self, job: JobDefinition) -> None:
        """持续调度单个 Job。

        每一轮：求解下一次计划触发时刻 → 分片睡眠到点前 0.5 秒 → 触发。
        分片（最多 600 秒一片）是为了让系统挂起、NTP 跳变、容器 suspend 之后能按墙钟重新对齐，
        而不是抱着一个过期的长 sleep 整周错位。首次执行前同样只等待到计划时刻，
        避免 daemon 启动瞬间立刻把所有任务跑一遍。
        """
        _logger.info("scheduler: run_loop started for %s", job.job_name)
        try:
            tz = ZoneInfo(job.timezone or DEFAULT_TIMEZONE)
        except Exception:
            tz = ZoneInfo(DEFAULT_TIMEZONE)

        last_fired: datetime | None = None
        # 进程在「运行记录已写入、配额还没对上」时崩溃的，这里按成功/未结束的计划时刻补记。
        if self._reconcile_runs_done(job):
            _logger.info("scheduler: run_loop ended for %s", job.job_name)
            return

        while self._running:
            # 防重复触发：_sleep_until 会提前 ~50ms 放行，若此刻立刻重算，
            # next_fire_at 仍会返回「同一个还没到的 target」，于是同一计划时刻被连触发多次。
            # 因此记住已触发的计划时刻，等墙钟真正越过它再继续求解。
            if last_fired is not None:
                now = datetime.now(tz)
                if now <= last_fired:
                    try:
                        await asyncio.sleep(
                            min((last_fired - now).total_seconds() + 0.02, 5.0)
                        )
                    except asyncio.CancelledError:
                        break
                    continue
            try:
                spec = effective_spec(job)
                now = datetime.now(tz)
                target = next_fire_at_for_job(job, now, tz)
            except Exception:
                _logger.exception(
                    "scheduler: resolve next fire time failed for %s, fallback 60s",
                    job.job_name,
                )
                spec, target = None, datetime.now(tz) + timedelta(seconds=60)
                now = datetime.now(tz)

            if await self._maybe_catch_up(job, spec, now, tz, last_fired):
                missed_now = previous_fire_at(spec, now, tz) if spec is not None else None
                if missed_now is not None:
                    last_fired = missed_now
                if not self._running or self._should_retire_after_fire(job, spec):
                    break
                continue

            if target is None:
                reason = exhaustion_reason(job, datetime.now(tz), tz)
                if not reason:
                    if job.one_shot or (spec is not None and spec.kind == "once"):
                        reason = "一次性任务已过期或已触发"
                    else:
                        reason = "未来 4 年内无命中（检查 weekdays/month_days/months/exceptions）"
                # 正常收尾也要停用。否则 watcher 把 loop 结束当成崩溃，每个刷新周期重启一次。
                self._disable_job(job, reason)
                break

            if not await self._sleep_until(target, tz):
                break

            last_fired = target
            outcome = await self._fire_scheduled_slot(job, target)
            if outcome == "stopped" or not self._running:
                break
            if self._should_retire_after_fire(job, spec):
                break
        _logger.info("scheduler: run_loop ended for %s", job.job_name)

    def _find_duplicate_run(
        self, job_name: str, scheduled_at: datetime
    ) -> Optional[JobRun]:
        """返回同一 job 在同一计划时刻已经占住的 run。

        失败不算重复，方便同一格重试。成功和尚未写完的记录都算占住，
        避免崩溃后续跑，也避免只扫全局最近 200 条时把更早的同刻运行漏掉。
        """
        try:
            for prev in self._job_run_repo.list_for_job(job_name):
                if prev.scheduled_at is None:
                    continue
                if _run_status(prev) == "failed":
                    continue
                if _same_instant(prev.scheduled_at, scheduled_at):
                    return prev
        except Exception:
            _logger.exception(
                "scheduler: duplicate-run check failed job=%s", job_name
            )
        return None

    async def _maybe_catch_up(
        self,
        job: JobDefinition,
        spec: Any,
        now: datetime,
        tz: Any,
        last_fired: Optional[datetime],
    ) -> bool:
        """catch_up 开启时补最近一个错过的时刻。返回 True 表示这一轮已经处理过补跑。"""
        if spec is None or not getattr(spec, "catch_up", False):
            return False
        try:
            missed = previous_fire_at(spec, now, tz)
        except Exception:
            _logger.exception("scheduler: previous fire time failed for %s", job.job_name)
            return False
        if missed is None:
            return False
        if last_fired is not None and _same_instant(last_fired, missed):
            return False
        if self._find_duplicate_run(job.job_name, missed) is not None:
            return False
        _logger.info(
            "scheduler: catch up job=%s missed=%s",
            job.job_name,
            missed.isoformat(),
        )
        await self._fire_scheduled_slot(job, missed)
        return True

    async def _fire_scheduled_slot(self, job: JobDefinition, target: datetime) -> str:
        """触发一个计划时刻。成功才记配额；失败在进程内重试同一格，重复触发不记配额。"""
        delays = _SLOT_RETRY_DELAYS_SECONDS
        for attempt in range(_SLOT_MAX_ATTEMPTS):
            if not self._running:
                return "stopped"
            before = self._find_duplicate_run(job.job_name, target)
            try:
                run = await self.run_job_once(job, scheduled_at=target)
            except asyncio.CancelledError:
                return "stopped"
            except Exception:
                _logger.exception(
                    "scheduler: run_job_once raised for %s", job.job_name
                )
                run = None
            if (
                run is not None
                and before is not None
                and run.run_id == before.run_id
            ):
                self._reconcile_runs_done(job)
                return "duplicate"
            if run is not None and _run_status(run) == "success":
                self._reconcile_runs_done(job)
                return "success"
            if attempt + 1 >= _SLOT_MAX_ATTEMPTS:
                _logger.warning(
                    "scheduler: job %s 计划时刻 %s 连续失败 %d 次，放弃这一格且不扣配额",
                    job.job_name,
                    target.isoformat(),
                    _SLOT_MAX_ATTEMPTS,
                )
                return "failed"
            delay = delays[min(attempt, len(delays) - 1)]
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return "stopped"
        return "failed"

    def _should_retire_after_fire(self, job: JobDefinition, spec: Any) -> bool:
        """一次性任务，或次数用尽，这一轮之后就该退出 loop。"""
        if not job.enabled:
            return True
        if job.one_shot or (spec is not None and getattr(spec, "kind", "") == "once"):
            self._disable_job(job, "一次性任务已结束")
            return True
        return self._reconcile_runs_done(job)

    def _disable_job(self, job: JobDefinition, reason: str) -> None:
        if not job.enabled:
            return
        _logger.info("scheduler: job %s 自动停用（%s）", job.job_name, reason)
        job.enabled = False
        self._persist_job_state(job)

    def _persist_job_state(self, job: JobDefinition) -> None:
        """把调度器改过的运行态（runs_done / enabled）写回仓库。"""
        try:
            job.updated_at = datetime.now()
            self._job_def_repo.update(job)
            snap = self._job_snapshots.get(job.job_name)
            if snap is not None:
                snap.runs_done = job.runs_done
                snap.enabled = job.enabled
        except Exception:
            _logger.exception(
                "scheduler: persist job state failed for %s", job.job_name
            )

    def _reconcile_runs_done(self, job: JobDefinition) -> bool:
        """按已占住的计划时刻把 runs_done 补齐。返回 True 表示配额已经用尽。

        只升不降：历史多记的次数不在这里改回去。没有 count 的任务不扫运行记录。
        重复触发不会多记，失败不记，崩溃前写下的成功/未结束记录会补记。
        """
        try:
            spec = effective_spec(job)
            if spec.count is None:
                return False
            done = self._count_consumed_slots(job.job_name)
            changed = False
            if done > int(getattr(job, "runs_done", 0) or 0):
                job.runs_done = done
                changed = True
            remaining = remaining_runs(job, spec)
            if remaining is not None and remaining <= 0:
                if job.enabled:
                    job.enabled = False
                    changed = True
                    _logger.info(
                        "scheduler: job %s 达 count=%s 上限，已自动停用",
                        job.job_name,
                        spec.count,
                    )
                if changed:
                    self._persist_job_state(job)
                return True
            if changed:
                self._persist_job_state(job)
        except Exception:
            _logger.exception(
                "scheduler: reconcile count quota failed for %s", job.job_name
            )
        return False

    def _count_consumed_slots(self, job_name: str) -> int:
        seen: set[int] = set()
        for run in self._job_run_repo.list_for_job(job_name):
            if run.scheduled_at is None or _run_status(run) == "failed":
                continue
            try:
                seen.add(int(run.scheduled_at.timestamp()))
            except Exception:
                continue
        return len(seen)

    async def _sleep_until(self, target: datetime, tz: Any) -> bool:
        """睡到 target 前 0.05 秒；返回 False 表示被取消或调度器停机。"""
        while self._running:
            remain = (target - datetime.now(tz)).total_seconds()
            if remain <= 0.05:
                return True
            try:
                await asyncio.sleep(min(remain - 0.05, 600.0))
            except asyncio.CancelledError:
                return False
        return False

    def _compute_sleep_seconds(self, job: JobDefinition) -> float:
        """下一次触发前应 sleep 的秒数（兼容保留）。

        语义已统一到 calendar.next_fire_at：优先结构化 schedule，缺失时按旧
        payload（times / start_time+interval / daily_time / interval）推导。
        """
        try:
            tz = ZoneInfo(job.timezone or DEFAULT_TIMEZONE)
        except Exception:
            tz = ZoneInfo(DEFAULT_TIMEZONE)
        spec = effective_spec(job)
        target = next_fire_at_for_job(job, datetime.now(tz), tz)
        if target is None:
            return max(1.0, float(job.interval_seconds or 60))
        return max(0.0, (target - datetime.now(tz)).total_seconds())



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
        # schedule 必须参与比较：否则改了 weekdays 之后 watcher 不会重启 loop，
        # 运行中的协程仍按旧规则计时，改了等于没改。
        if (old.schedule or {}) != (new.schedule or {}):
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

                    need_start = False
                    reason = ""
                    if existing_task is None:
                        need_start = True
                        reason = "new"
                    elif existing_task.done():
                        if existing_task.cancelled():
                            exc = None
                        else:
                            exc = existing_task.exception()
                        fresh = self._job_def_repo.get(job.job_name)
                        still_enabled = fresh is not None and fresh.enabled
                        if exc is None:
                            # 正常收尾不是崩溃。规则没自己停用时这里补一刀，避免每个刷新周期重启。
                            if still_enabled:
                                self._disable_job(fresh, "调度循环已正常结束")
                            continue
                        _logger.warning(
                            "scheduler: run_loop for %s died (exc=%s), restarting",
                            job.job_name,
                            exc,
                        )
                        need_start = True
                        reason = "crashed"
                    elif snapshot is not None and self._job_changed(snapshot, job):
                        existing_task.cancel()
                        need_start = True
                        reason = "config_changed"

                    if need_start:
                        self._tasks[job.job_name] = asyncio.create_task(
                            self._run_loop(job),
                            name=f"scheduler:{job.job_name}",
                        )
                        try:
                            self._job_snapshots[job.job_name] = job.model_copy(deep=True)
                        except Exception:  # pragma: no cover
                            self._job_snapshots[job.job_name] = job
                        if reason != "new":
                            _logger.info(
                                "scheduler: restarted run_loop for %s (reason=%s)",
                                job.job_name,
                                reason,
                            )

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
        tool_template = str(payload.get("tool_template") or "").strip() or None
        feishu_chat_id = str(payload.get("feishu_chat_id") or "").strip() or None
        remote_login = str(payload.get("remote_login") or "").strip() or None
        remote_path = str(payload.get("remote_path") or "").strip() or "~"
        remote_profile = str(payload.get("remote_profile") or "").strip() or "dev"
        remote_ttl_raw = payload.get("remote_ttl_seconds")
        remote_ttl_seconds: Optional[int] = None
        if remote_ttl_raw is not None:
            try:
                remote_ttl_seconds = int(remote_ttl_raw)
            except (TypeError, ValueError):
                remote_ttl_seconds = None
        remote_required = bool(payload.get("remote_required", True))

        task = make_cron_task(
            job.job_name,
            instruction=instruction,
            user_id=user_id,
            memory_owner=memory_owner or None,
            core_mode=core_mode,
            tool_template=tool_template,
            feishu_chat_id=feishu_chat_id,
            write_recent_topic=bool(payload.get("write_recent_topic", False)),
            remote_login=remote_login,
            remote_path=remote_path,
            remote_profile=remote_profile,
            remote_ttl_seconds=remote_ttl_seconds,
            remote_required=remote_required,
        )
        await asyncio.to_thread(self._task_queue.push, task)
