"""calendar.ScheduleSpec 的单测：基础语义 + 有界规则（until / count / exceptions）。"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from system.automation.calendar import (
    ScheduleSpec,
    ScheduleSpecError,
    describe_schedule,
    exhaustion_reason,
    next_fire_at,
    next_fire_at_for_job,
    normalize_count,
    normalize_exceptions,
    normalize_until,
    remaining_runs,
)

TZ = ZoneInfo("Asia/Shanghai")


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


def _job(spec: ScheduleSpec, runs_done: int = 0) -> SimpleNamespace:
    return SimpleNamespace(schedule=spec.to_dict(), runs_done=runs_done)


# -- 基础语义 ---------------------------------------------------------------


def test_weekly_monday_next_fire() -> None:
    spec = ScheduleSpec(kind="calendar", times=["07:35"], weekdays=[1])
    # 2026-10-20 是周二，下一次应为 2026-10-26 周一
    nxt = next_fire_at(spec, _dt("2026-10-20T03:28:00"), TZ)
    assert nxt == _dt("2026-10-26T07:35:00")


def test_multiple_times_same_day() -> None:
    spec = ScheduleSpec(kind="calendar", times=["08:00", "20:00"])
    now = _dt("2026-10-20T09:00:00")
    assert next_fire_at(spec, now, TZ) == _dt("2026-10-20T20:00:00")
    assert next_fire_at(spec, _dt("2026-10-20T20:00:00"), TZ) == _dt("2026-10-21T08:00:00")


def test_monthly_first_day() -> None:
    spec = ScheduleSpec(kind="calendar", times=["20:00"], month_days=[1])
    assert next_fire_at(spec, _dt("2026-10-20T03:28:00"), TZ) == _dt(
        "2026-11-01T20:00:00"
    )


# -- exceptions -------------------------------------------------------------


def test_exceptions_skip_this_week() -> None:
    spec = ScheduleSpec(
        kind="calendar", times=["07:35"], weekdays=[1], exceptions=("2026-10-26",)
    )
    assert next_fire_at(spec, _dt("2026-10-20T03:28:00"), TZ) == _dt(
        "2026-11-02T07:35:00"
    )


def test_exceptions_do_not_hide_later_time_same_day() -> None:
    spec = ScheduleSpec(
        kind="calendar", times=["08:00", "20:00"], exceptions=("2026-10-21",)
    )
    # 跳过 10-21 后，从 10-20 20:00 往后应是 10-22 08:00
    assert next_fire_at(spec, _dt("2026-10-20T20:00:00"), TZ) == _dt(
        "2026-10-22T08:00:00"
    )


def test_all_days_skipped_returns_none() -> None:
    spec = ScheduleSpec(
        kind="calendar",
        times=["07:35"],
        weekdays=[1],
        exceptions=tuple(
            f"2026-{m:02d}-{d:02d}" for m, d in [(10, 26), (11, 2), (11, 9)]
        ),
    )
    # 只跳了三次，仍应能返回 11-16
    assert next_fire_at(spec, _dt("2026-10-20T03:28:00"), TZ) == _dt(
        "2026-11-16T07:35:00"
    )


# -- until ------------------------------------------------------------------


def test_until_cuts_calendar() -> None:
    spec = ScheduleSpec(
        kind="calendar",
        times=["21:00"],
        weekdays=[7],
        until=normalize_until("2026-10-25"),
    )
    # until 是 10-25 23:59:59，10-25 21:00 仍应触发
    assert next_fire_at(spec, _dt("2026-10-20T03:28:00"), TZ) == _dt(
        "2026-10-25T21:00:00"
    )
    # 过了那次之后就没有了
    assert next_fire_at(spec, _dt("2026-10-25T21:00:00"), TZ) is None


def test_until_cuts_interval() -> None:
    spec = ScheduleSpec(
        kind="interval", interval_seconds=3600, until=_dt("2026-10-20T05:00:00")
    )
    assert next_fire_at(spec, _dt("2026-10-20T03:28:00"), TZ) == _dt(
        "2026-10-20T04:28:00"
    )
    assert next_fire_at(spec, _dt("2026-10-20T04:30:00"), TZ) is None


def test_until_filters_expired_once() -> None:
    spec = ScheduleSpec(
        kind="once",
        run_at=_dt("2026-10-20T05:00:00"),
        until=_dt("2026-10-20T04:00:00"),
    )
    assert next_fire_at(spec, _dt("2026-10-20T03:28:00"), TZ) is None


def test_normalize_until_accepts_date_and_iso() -> None:
    assert normalize_until("2026-12-31") == datetime(2026, 12, 31, 23, 59, 59)
    assert normalize_until("2026-12-31T18:00:00+08:00") == datetime.fromisoformat(
        "2026-12-31T18:00:00+08:00"
    )
    assert normalize_until(None) is None
    with pytest.raises(ScheduleSpecError):
        normalize_until("明年")


# -- count ------------------------------------------------------------------


def test_count_quota_via_job() -> None:
    spec = ScheduleSpec(kind="calendar", times=["21:00"], weekdays=[7], count=3)
    job = _job(spec, runs_done=0)
    assert remaining_runs(job, spec) == 3
    assert next_fire_at_for_job(job, _dt("2026-10-20T03:28:00"), TZ) == _dt(
        "2026-10-25T21:00:00"
    )

    job.runs_done = 3
    assert remaining_runs(job, spec) == 0
    assert next_fire_at_for_job(job, _dt("2026-10-20T03:28:00"), TZ) is None
    assert exhaustion_reason(job, _dt("2026-10-20T03:28:00"), TZ) == "count 已用尽（共 3 次）"


def test_count_without_quota_is_unlimited() -> None:
    spec = ScheduleSpec(kind="calendar", times=["21:00"], weekdays=[7])
    job = _job(spec, runs_done=99)
    assert remaining_runs(job, spec) is None
    assert next_fire_at_for_job(job, _dt("2026-10-20T03:28:00"), TZ) == _dt(
        "2026-10-25T21:00:00"
    )


def test_normalize_count_validation() -> None:
    assert normalize_count("5") == 5
    assert normalize_count(None) is None
    with pytest.raises(ScheduleSpecError):
        normalize_count(0)
    with pytest.raises(ScheduleSpecError):
        normalize_count("三次")


def test_exhaustion_reason_reports_until() -> None:
    spec = ScheduleSpec(
        kind="calendar", times=["21:00"], weekdays=[7], until=normalize_until("2026-10-01")
    )
    job = _job(spec)
    reason = exhaustion_reason(job, _dt("2026-10-20T03:28:00"), TZ)
    assert reason is not None and "until" in reason


# -- 归一化 / 序列化 / 展示 --------------------------------------------------


def test_normalize_exceptions_three_forms() -> None:
    assert normalize_exceptions("2026-10-21,2026-10-22") == ("2026-10-21", "2026-10-22")
    assert normalize_exceptions(["2026-10-21"]) == ("2026-10-21",)
    assert normalize_exceptions([{"skip": "2026-10-21", "reason": "停课"}]) == (
        "2026-10-21",
    )
    assert normalize_exceptions(None) == ()
    with pytest.raises(ScheduleSpecError):
        normalize_exceptions(["10/21"])


def test_roundtrip_dict() -> None:
    spec = ScheduleSpec(
        kind="calendar",
        times=["21:00"],
        weekdays=[7],
        until=_dt("2026-12-31T23:59:59"),
        count=12,
        exceptions=("2026-10-25",),
    )
    parsed = ScheduleSpec.from_dict(spec.to_dict())
    assert parsed is not None
    assert parsed.times == ["21:00"]
    assert parsed.weekdays == [7]
    assert parsed.count == 12
    assert parsed.exceptions == ("2026-10-25",)
    assert parsed.until == _dt("2026-12-31T23:59:59")


def test_describe_mentions_bounds() -> None:
    spec = ScheduleSpec(
        kind="calendar",
        times=["21:00"],
        weekdays=[7],
        until=normalize_until("2026-12-31"),
        count=12,
        exceptions=("2026-10-25",),
    )
    text = describe_schedule(spec)
    assert "每周日 21:00" in text
    assert "至 2026-12-31" in text
    assert "共 12 次" in text
    assert "跳过 1 天" in text


def test_legacy_job_has_no_bounds() -> None:
    """老任务（无 schedule 字段）不受影响：无 until/count/exceptions。"""
    job = SimpleNamespace(
        schedule=None,
        payload_template={"daily_time": "07:00"},
        interval_seconds=86400,
        one_shot=False,
        run_at=None,
    )
    spec = ScheduleSpec.from_legacy_job(job)
    assert spec.kind == "calendar"
    assert spec.until is None and spec.count is None and spec.exceptions == ()
