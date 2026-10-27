"""计划时刻的配额、幂等、补跑和无法命中时的停用。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from system.automation.calendar import ScheduleSpec, previous_fire_at
from system.automation.repositories import JobDefinitionRepository, JobRunRepository
from system.automation.scheduler import AutomationScheduler
from system.automation.types import JobDefinition, JobRun, JobStatus

TZ = ZoneInfo("Asia/Shanghai")


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _scheduler(tmp_path):
    repo_dir = str(tmp_path / "automation")
    job_def_repo = JobDefinitionRepository(base_dir=repo_dir)
    job_run_repo = JobRunRepository(base_dir=repo_dir)
    scheduler = AutomationScheduler(
        job_def_repo=job_def_repo,
        job_run_repo=job_run_repo,
    )
    return scheduler, job_def_repo, job_run_repo


def test_previous_fire_skips_hours_before_anchor() -> None:
    """start_time 之前，补跑不能落到当天锚点算不出来的整点。"""
    spec = ScheduleSpec(kind="interval", interval_seconds=3600, start_time="08:00")
    missed = previous_fire_at(spec, _dt("2026-10-22T07:59:00"), TZ)
    assert missed == _dt("2026-10-22T00:00:00")


def test_previous_fire_on_anchored_grid() -> None:
    spec = ScheduleSpec(kind="interval", interval_seconds=6 * 3600, start_time="08:00")
    assert previous_fire_at(spec, _dt("2026-10-22T15:10:00"), TZ) == _dt("2026-10-22T14:00:00")
    assert previous_fire_at(spec, _dt("2026-10-22T03:00:00"), TZ) == _dt("2026-10-22T02:00:00")


def test_previous_calendar_fire_is_latest_slot() -> None:
    spec = ScheduleSpec(kind="calendar", times=["08:00", "20:00"])
    missed = previous_fire_at(spec, _dt("2026-10-22T12:00:00"), TZ)
    assert missed == _dt("2026-10-22T08:00:00")


@pytest.mark.asyncio
async def test_duplicate_fire_does_not_consume_count(tmp_path) -> None:
    scheduler, job_def_repo, _runs = _scheduler(tmp_path)
    when = _dt("2026-10-22T08:00:00")
    job = JobDefinition(
        job_name="counted",
        job_type="human",
        interval_seconds=3600,
        timezone="Asia/Shanghai",
        schedule=ScheduleSpec(
            kind="calendar", times=["08:00"], count=3
        ).to_dict(),
        runs_done=0,
    )
    job_def_repo.create(job)
    scheduler._running = True

    first = await scheduler._fire_scheduled_slot(job, when)
    second = await scheduler._fire_scheduled_slot(job, when)

    assert first == "success"
    assert second == "duplicate"
    stored = job_def_repo.get(job.job_name)
    assert stored is not None
    assert stored.runs_done == 1


@pytest.mark.asyncio
async def test_duplicate_found_beyond_global_recent_runs(tmp_path) -> None:
    scheduler, _defs, runs = _scheduler(tmp_path)
    when = datetime.now(TZ).replace(microsecond=0)
    for i in range(205):
        runs.create(
            JobRun(
                job_name=f"other-{i}",
                job_type="human",
                scheduled_at=when,
                status=JobStatus.SUCCESS,
            )
        )
    runs.create(
        JobRun(
            job_name="target",
            job_type="human",
            scheduled_at=when - timedelta(days=30),
            status=JobStatus.SUCCESS,
        )
    )
    found = scheduler._find_duplicate_run("target", when - timedelta(days=30))
    assert found is not None
    assert found.job_name == "target"


@pytest.mark.asyncio
async def test_failed_slot_retries_then_does_not_consume(tmp_path, monkeypatch) -> None:
    scheduler, job_def_repo, runs = _scheduler(tmp_path)
    job = JobDefinition(
        job_name="flaky",
        job_type="human",
        interval_seconds=3600,
        timezone="Asia/Shanghai",
        schedule=ScheduleSpec(kind="calendar", times=["08:00"], count=2).to_dict(),
    )
    job_def_repo.create(job)

    async def fail_dispatch(_job):
        raise RuntimeError("upstream down")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(scheduler, "_dispatch_job", fail_dispatch)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    scheduler._running = True
    when = _dt("2026-10-22T08:00:00")

    outcome = await scheduler._fire_scheduled_slot(job, when)

    assert outcome == "failed"
    assert job.runs_done == 0
    failed = [r for r in runs.list_for_job("flaky") if r.status == JobStatus.FAILED]
    assert len(failed) == 3


@pytest.mark.asyncio
async def test_reconcile_counts_run_written_before_crash(tmp_path) -> None:
    scheduler, job_def_repo, runs = _scheduler(tmp_path)
    when = _dt("2026-10-22T08:00:00")
    job = JobDefinition(
        job_name="crashed",
        job_type="human",
        interval_seconds=3600,
        timezone="Asia/Shanghai",
        schedule=ScheduleSpec(kind="calendar", times=["08:00"], count=2).to_dict(),
        runs_done=0,
    )
    job_def_repo.create(job)
    runs.create(
        JobRun(
            job_name="crashed",
            job_type="human",
            scheduled_at=when,
            status=JobStatus.SUCCESS,
        )
    )

    exhausted = scheduler._reconcile_runs_done(job)

    assert exhausted is False
    assert job.runs_done == 1
    assert job_def_repo.get("crashed").runs_done == 1


@pytest.mark.asyncio
async def test_impossible_calendar_disables_job(tmp_path) -> None:
    scheduler, job_def_repo, _runs = _scheduler(tmp_path)
    job = JobDefinition(
        job_name="never",
        job_type="human",
        interval_seconds=86400,
        timezone="Asia/Shanghai",
        enabled=True,
        schedule=ScheduleSpec(
            kind="calendar", times=["08:00"], months=[2], month_days=[31]
        ).to_dict(),
    )
    job_def_repo.create(job)
    scheduler._running = True

    await scheduler._run_loop(job)

    stored = job_def_repo.get("never")
    assert stored is not None
    assert stored.enabled is False


@pytest.mark.asyncio
async def test_watcher_does_not_restart_clean_exit(tmp_path, monkeypatch) -> None:
    scheduler, job_def_repo, _runs = _scheduler(tmp_path)
    job = JobDefinition(
        job_name="finished",
        job_type="human",
        interval_seconds=3600,
        timezone="Asia/Shanghai",
        enabled=True,
        schedule=ScheduleSpec(kind="calendar", times=["08:00"]).to_dict(),
    )
    job_def_repo.create(job)

    async def finished():
        return None

    task = asyncio.create_task(finished())
    await task
    scheduler._tasks[job.job_name] = task
    scheduler._job_snapshots[job.job_name] = job
    scheduler._reload_interval = 0
    scheduler._running = True

    async def stop_after_one(_delay):
        scheduler._running = False

    monkeypatch.setattr(
        "system.automation.config_sync.sync_job_definitions_from_config",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr(asyncio, "sleep", stop_after_one)

    await scheduler._watch_job_definitions()

    assert job_def_repo.get("finished").enabled is False
    assert scheduler._tasks[job.job_name] is task


@pytest.mark.asyncio
async def test_catch_up_fires_latest_missed_slot_once(tmp_path, monkeypatch) -> None:
    scheduler, job_def_repo, runs = _scheduler(tmp_path)
    job = JobDefinition(
        job_name="catch",
        job_type="human",
        interval_seconds=86400,
        timezone="Asia/Shanghai",
        enabled=True,
        schedule=ScheduleSpec(
            kind="calendar", times=["08:00"], catch_up=True, count=5
        ).to_dict(),
    )
    job_def_repo.create(job)

    async def stop_sleep(_self, _target, _tz):
        scheduler._running = False
        return False

    monkeypatch.setattr(AutomationScheduler, "_sleep_until", stop_sleep)
    scheduler._running = True

    await scheduler._run_loop(job)

    caught = runs.list_for_job("catch")
    assert len(caught) == 1
    assert caught[0].status == JobStatus.SUCCESS
    assert caught[0].scheduled_at is not None
    assert job_def_repo.get("catch").runs_done == 1
