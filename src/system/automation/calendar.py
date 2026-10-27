"""日历式触发规则：把 JobDefinition 的调度语义统一成可计算、可展示的 ScheduleSpec。

设计边界（刻意保持很小）：

- 只回答「下一次什么时候触发」这一个问题：``next_fire_at`` 是纯函数，无副作用、可离线单测。
- 语义字段与给 Agent 的 payload_template 分离：payload 是提词，schedule 是数据。
- 不使用 cron 字符串：DOM/DOW 在 cron 里是 OR 语义、时区边界要自己解析，模型很容易填错一格。
  这里用正交字段（times / weekdays / month_days / months），None 表示不限制。
- 老任务不迁移：``schedule`` 缺失时由 ``from_legacy_job()`` 按原 payload 语义推导，行为等价。
- 不物化实例。``catch_up`` 为真时，调度器只补最近一个已经错过的时刻，不回放整个空档。

有界规则（可选，任一 kind 均可叠加）：

- ``until``      : 到期时刻，超过它不再触发（等价 RRULE 的 UNTIL）。给纯日期时按当日 23:59:59 理解。
- ``count``      : 总共最多触发 N 次（等价 RRULE 的 COUNT）；已跑次数记在 JobDefinition.runs_done，
                   因此 ``next_fire_at`` 仍是纯函数，次数判断放在 ``next_fire_at_for_job``。
- ``exceptions`` : 跳过的日期（YYYY-MM-DD），等价 iCalendar 的 EXDATE；只对 calendar 生效。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Asia/Shanghai"

# calendar 扫描上限：需要覆盖 2 月 29 日这种最长约 4 年的空档。
_SCAN_DAYS = 366 * 4 + 1

_VALID_KINDS = ("once", "interval", "calendar")


class ScheduleSpecError(ValueError):
    """调度规则非法（字段越界、kind 未知、calendar 缺 times 等）。"""


# ---------------------------------------------------------------------------
# 归一化工具
# ---------------------------------------------------------------------------


def normalize_times(raw: Any) -> Optional[List[str]]:
    """把 "08:00,14:30" / ["08:00", "14:30"] 归一化为 ["08:00", "14:30"]（去重保序）。

    非法项抛 ScheduleSpecError，避免静默吞掉用户写错的时刻。
    """
    if raw is None:
        return None
    items: List[Any]
    if isinstance(raw, str):
        items = [s for s in raw.replace("，", ",").split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        raise ScheduleSpecError(f"times 必须是字符串或列表，收到 {type(raw).__name__}")

    out: List[str] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        parsed = _parse_hm(text)
        if parsed is None:
            raise ScheduleSpecError(f"非法时刻 {text!r}，应为 HH:MM（00:00-23:59）")
        norm = f"{parsed[0]:02d}:{parsed[1]:02d}"
        if norm not in out:
            out.append(norm)
    return out or None


def normalize_ints(
    raw: Any,
    low: int,
    high: int,
    field_name: str,
) -> Optional[List[int]]:
    """归一化整数字段（weekdays / month_days / months），越界即报错。"""
    if raw is None:
        return None
    items: List[Any]
    if isinstance(raw, str):
        items = [s for s in raw.replace("，", ",").split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]

    out: List[int] = []
    for item in items:
        if isinstance(item, str) and not item.strip():
            continue
        try:
            value = int(str(item).strip())
        except (TypeError, ValueError):
            raise ScheduleSpecError(
                f"{field_name} 只能包含整数（{low}-{high}），收到 {item!r}"
            )
        if not low <= value <= high:
            raise ScheduleSpecError(f"{field_name} 取值必须在 {low}-{high}，收到 {value}")
        if value not in out:
            out.append(value)
    return sorted(out) or None


def _parse_hm(text: Any) -> Optional[Tuple[int, int]]:
    """解析 HH:MM（也容忍 H:MM），非法返回 None。"""
    if not isinstance(text, str):
        return None
    body = text.strip()
    if not body or ":" not in body:
        return None
    h_str, m_str = body.split(":", 1)
    try:
        hour, minute = int(h_str), int(m_str)
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def _parse_run_at(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ScheduleSpecError(f"run_at 必须是 ISO-8601 时间，收到 {text!r}")


def _parse_date(raw: Any) -> Optional[date]:
    """解析 YYYY-MM-DD；非法返回 None。"""
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def normalize_until(raw: Any) -> Optional[datetime]:
    """归一化 until：接受 ISO 时间 / YYYY-MM-DD（后者按当天 23:59:59 收尾）。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    # 纯日期要按「当天结束」处理；注意 Python 3.11+ 的 fromisoformat("2026-12-31")
    # 会返回 00:00 而不报错，所以必须先判定是不是纯日期。
    parsed_date = _parse_date(text)
    if parsed_date is not None:
        return datetime.combine(parsed_date, time(hour=23, minute=59, second=59))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ScheduleSpecError(f"until 必须是 ISO-8601 时间或 YYYY-MM-DD 日期，收到 {text!r}")


def normalize_count(raw: Any) -> Optional[int]:
    """归一化 count：正整数，表示该规则总共最多触发多少次。"""
    if raw is None or raw == "":
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ScheduleSpecError(f"count 必须是正整数，收到 {raw!r}")
    if value <= 0:
        raise ScheduleSpecError(f"count 必须为正整数，收到 {value}")
    return value


def normalize_exceptions(raw: Any) -> Tuple[str, ...]:
    """归一化 exceptions（跳过的日期）。

    接受三种写法，统一成排好序的 ``("2026-10-20", ...)``：
      - ``"2026-10-20,2026-10-21"``
      - ``["2026-10-20", "2026-10-21"]``
      - ``[{"skip": "2026-10-20"}, {"date": "2026-10-21", "reason": "..."}]``
    """
    if raw is None or raw == "":
        return ()
    items: List[Any]
    if isinstance(raw, str):
        items = raw.replace("，", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]

    out: List[str] = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("skip") or item.get("date") or item.get("skip_date")
        else:
            value = item
        text = str(value).strip() if value is not None else ""
        if not text:
            continue
        parsed = _parse_date(text)
        if parsed is None:
            raise ScheduleSpecError(f"exceptions 里的日期非法 {text!r}，应为 YYYY-MM-DD")
        norm = parsed.isoformat()
        if norm not in out:
            out.append(norm)
    return tuple(sorted(out))



# ---------------------------------------------------------------------------
# 规则对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleSpec:
    """一条触发规则。

    kind:
      - ``once``     : run_at 指定的单次触发
      - ``interval`` : 每 interval_seconds 一次；可选 start_time 作为当天锚点
      - ``calendar`` : times 必填；weekdays / month_days / months 为可选限制
                       （weekdays 用 ISO 编号 1=周一 … 7=周日）
    """

    kind: str = "interval"
    times: Optional[List[str]] = None
    weekdays: Optional[List[int]] = None
    month_days: Optional[List[int]] = None
    months: Optional[List[int]] = None
    interval_seconds: Optional[int] = None
    start_time: Optional[str] = None
    run_at: Optional[datetime] = None
    catch_up: bool = False
    # 有界规则：到期/够次数/跳过某些天。三者对任意 kind 都可叠加（exceptions 只对 calendar 生效）。
    until: Optional[datetime] = None
    count: Optional[int] = None
    exceptions: Tuple[str, ...] = ()

    # -- 构造 ------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Any) -> Optional[ScheduleSpec]:
        """从落盘的 dict 解析；空值返回 None（表示走 legacy 推导）。"""
        if not data:
            return None
        if isinstance(data, ScheduleSpec):
            return data
        if not isinstance(data, dict):
            raise ScheduleSpecError(f"schedule 必须是对象，收到 {type(data).__name__}")

        run_at = _parse_run_at(data.get("run_at"))
        times = normalize_times(data.get("times"))
        weekdays = normalize_ints(data.get("weekdays"), 1, 7, "weekdays")
        month_days = normalize_ints(data.get("month_days"), 1, 31, "month_days")
        months = normalize_ints(data.get("months"), 1, 12, "months")
        start_time_raw = data.get("start_time")
        start_time = None
        if start_time_raw:
            parsed = _parse_hm(str(start_time_raw))
            if parsed is None:
                raise ScheduleSpecError(f"start_time 必须是 HH:MM，收到 {start_time_raw!r}")
            start_time = f"{parsed[0]:02d}:{parsed[1]:02d}"

        interval_seconds: Optional[int] = None
        if data.get("interval_seconds") is not None:
            try:
                interval_seconds = int(data["interval_seconds"])
            except (TypeError, ValueError):
                raise ScheduleSpecError("interval_seconds 必须是整数")
            if interval_seconds <= 0:
                raise ScheduleSpecError("interval_seconds 必须为正数")

        until = normalize_until(data.get("until"))
        count = normalize_count(data.get("count"))
        exceptions = normalize_exceptions(data.get("exceptions"))
        bound = {"until": until, "count": count, "exceptions": exceptions}

        kind = data.get("kind")
        kind = str(kind).strip().lower() if kind else ""
        if not kind:
            if run_at is not None:
                kind = "once"
            elif times:
                kind = "calendar"
            elif interval_seconds:
                kind = "interval"
            elif start_time:
                kind = "calendar"
            else:
                raise ScheduleSpecError("schedule 无法推断 kind，请显式指定 once/interval/calendar")
        if kind not in _VALID_KINDS:
            raise ScheduleSpecError(f"未知 kind {kind!r}，可选 {list(_VALID_KINDS)}")

        if kind == "once":
            if run_at is None:
                raise ScheduleSpecError("kind=once 必须提供 run_at")
            return cls(
                kind="once",
                run_at=run_at,
                catch_up=bool(data.get("catch_up", False)),
                **bound,
            )

        if kind == "interval":
            if not interval_seconds:
                raise ScheduleSpecError("kind=interval 必须提供正整数 interval_seconds")
            return cls(
                kind="interval",
                interval_seconds=interval_seconds,
                start_time=start_time,
                catch_up=bool(data.get("catch_up", False)),
                **bound,
            )

        # calendar
        if not times:
            raise ScheduleSpecError("kind=calendar 必须提供非空 times（HH:MM 列表）")
        return cls(
            kind="calendar",
            times=times,
            weekdays=weekdays,
            month_days=month_days,
            months=months,
            catch_up=bool(data.get("catch_up", False)),
            **bound,
        )

    @classmethod
    def from_legacy_job(cls, job: Any) -> ScheduleSpec:
        """按旧 payload 语义推导规则（优先级与历史 _compute_sleep_seconds 一致）。"""
        payload: Dict[str, Any] = getattr(job, "payload_template", None) or {}
        run_at = getattr(job, "run_at", None)
        if getattr(job, "one_shot", False) and run_at is not None:
            return cls(kind="once", run_at=run_at)

        times = normalize_times(payload.get("times"))
        if times:
            return cls(kind="calendar", times=times)

        interval_seconds = int(getattr(job, "interval_seconds", 0) or 0)
        start_time_raw = payload.get("start_time")
        if start_time_raw:
            parsed = _parse_hm(str(start_time_raw))
            if parsed is not None and interval_seconds > 0:
                return cls(
                    kind="interval",
                    interval_seconds=interval_seconds,
                    start_time=f"{parsed[0]:02d}:{parsed[1]:02d}",
                )

        daily_time = payload.get("daily_time")
        if daily_time:
            parsed = _parse_hm(str(daily_time))
            if parsed is not None:
                return cls(kind="calendar", times=[f"{parsed[0]:02d}:{parsed[1]:02d}"])

        return cls(kind="interval", interval_seconds=max(1, interval_seconds or 60))

    def to_dict(self) -> Dict[str, Any]:
        """落盘用：只输出与 kind 相关的字段，避免脏字段堆积。"""
        data: Dict[str, Any] = {"kind": self.kind}
        if self.kind == "once":
            if self.run_at is not None:
                data["run_at"] = self.run_at.isoformat()
        elif self.kind == "interval":
            data["interval_seconds"] = int(self.interval_seconds or 60)
            if self.start_time:
                data["start_time"] = self.start_time
        else:
            data["times"] = list(self.times or [])
            if self.weekdays:
                data["weekdays"] = list(self.weekdays)
            if self.month_days:
                data["month_days"] = list(self.month_days)
            if self.months:
                data["months"] = list(self.months)
        if self.catch_up:
            data["catch_up"] = True
        if self.until is not None:
            data["until"] = self.until.isoformat()
        if self.count is not None:
            data["count"] = int(self.count)
        if self.exceptions:
            data["exceptions"] = list(self.exceptions)
        return data

    # -- 展示 ------------------------------------------------------------

    def describe(self) -> str:
        base = self._describe_base()
        bounds: List[str] = []
        if self.until is not None:
            bounds.append(f"至 {self.until:%Y-%m-%d}")
        if self.count is not None:
            bounds.append(f"共 {self.count} 次")
        if self.exceptions:
            bounds.append(f"跳过 {len(self.exceptions)} 天")
        if bounds:
            return f"{base}（{'，'.join(bounds)}）"
        return base

    def _describe_base(self) -> str:
        if self.kind == "once":
            if self.run_at is None:
                return "一次性（未设置 run_at）"
            return f"一次性 {self.run_at:%Y-%m-%d %H:%M}"
        if self.kind == "interval":
            text = f"每 {_human_interval(int(self.interval_seconds or 60))}"
            if self.start_time:
                text += f"（{self.start_time} 起）"
            return text
        if not self.times:
            return "日历规则（未设置 times）"
        return f"{self._describe_days()} {'、'.join(self.times)}"

    def _describe_days(self) -> str:
        parts: List[str] = []
        if self.months:
            parts.append("、".join(f"{m} 月" for m in self.months))
        if self.month_days:
            parts.append("、".join(f"{d} 号" for d in self.month_days))
        weekdays = self.weekdays
        if weekdays:
            if sorted(weekdays) == [1, 2, 3, 4, 5]:
                parts.append("工作日")
            elif sorted(weekdays) == [6, 7]:
                parts.append("周末")
            elif len(weekdays) == 7:
                parts.append("每天")
            else:
                names = "、".join(_WEEKDAY_NAMES[d] for d in sorted(weekdays))
                parts.append(f"每{names}")
        if not parts:
            return "每天"
        return " ".join(parts)


_WEEKDAY_NAMES = {
    1: "周一",
    2: "周二",
    3: "周三",
    4: "周四",
    5: "周五",
    6: "周六",
    7: "周日",
}


def _human_interval(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds % 86400 == 0:
        return f"{seconds // 86400} 天"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


# ---------------------------------------------------------------------------
# 求解下一次触发时刻
# ---------------------------------------------------------------------------


def effective_spec(job: Any) -> ScheduleSpec:
    """取 job 的有效规则：优先结构化 schedule，缺失时按 legacy payload 推导。"""
    raw = getattr(job, "schedule", None)
    if raw:
        try:
            spec = ScheduleSpec.from_dict(raw)
            if spec is not None:
                return spec
        except ScheduleSpecError:
            _logger.warning(
                "scheduler: job %s 的 schedule 非法，回退 legacy 推导：%r",
                getattr(job, "job_name", "?"),
                raw,
            )
    return ScheduleSpec.from_legacy_job(job)


def next_fire_at(spec: ScheduleSpec, now: datetime, tz: tzinfo) -> Optional[datetime]:
    """返回严格大于 now 的下一次触发时刻；无解（过期 one-shot / 永不满足）返回 None。

    纯函数：不读时钟、不写状态，便于单测与 dry-run 对比。
    """
    now = _as_tz(now, tz)
    until = _as_tz(spec.until, tz) if spec.until is not None else None

    if spec.kind == "once":
        if spec.run_at is None:
            return None
        target = _as_tz(spec.run_at, tz)
        if until is not None and target > until:
            return None
        return target if target > now else None

    if spec.kind == "interval":
        period = max(1, int(spec.interval_seconds or 60))
        if spec.start_time:
            hm = _parse_hm(spec.start_time)
            if hm is not None:
                # 与历史实现一致：每天以当天 start_time 为锚点，按 period 向前推进。
                anchor = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
                if anchor > now:
                    target = anchor
                else:
                    steps = int((now - anchor).total_seconds() // period) + 1
                    target = anchor + timedelta(seconds=steps * period)
                if until is not None and target > until:
                    return None
                return target
        target = now + timedelta(seconds=period)
        if until is not None and target > until:
            return None
        return target

    # calendar
    parsed_times = _parse_times(spec.times or [])
    if not parsed_times:
        return None
    start_day = now.date()
    for offset in range(_SCAN_DAYS):
        current_day = start_day + timedelta(days=offset)
        # 早于 now 的日子不可能有候选；只有越过 until 才提前收手（候选按天单调递增）。
        if until is not None and current_day > until.date():
            return None
        if not _day_allowed(spec, current_day):
            continue
        for hour, minute in parsed_times:
            candidate = datetime.combine(
                current_day, time(hour=hour, minute=minute), tzinfo=tz
            )
            if until is not None and candidate > until:
                return None
            if candidate > now:
                return candidate
    return None


def previous_fire_at(spec: ScheduleSpec, now: datetime, tz: tzinfo) -> Optional[datetime]:
    """最近一个已经到期（<= now）的计划时刻。

    只给最近一格，供 catch_up 补跑；不回放更早的空档。
    没有 start_time 的 interval 没有墙上时钟格点，返回 None。
    """
    now = _as_tz(now, tz)
    until = _as_tz(spec.until, tz) if spec.until is not None else None

    if spec.kind == "once":
        if spec.run_at is None:
            return None
        target = _as_tz(spec.run_at, tz)
        if target > now:
            return None
        if until is not None and target > until:
            return None
        return target

    if spec.kind == "interval":
        if not spec.start_time:
            return None
        hm = _parse_hm(spec.start_time)
        if hm is None:
            return None
        period = max(1, int(spec.interval_seconds or 60))
        anchor = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if now >= anchor:
            steps = int((now - anchor).total_seconds() // period)
            candidate = anchor + timedelta(seconds=steps * period)
        else:
            # 过了零点、还没到今天的 start_time 时，next_fire 会直接跳到今天的锚点。
            # 能补的只有「昨天锚点按 period 往前推、并且在昨天结束前就算得出来」的最后一格。
            yesterday = anchor - timedelta(days=1)
            end_yesterday = anchor.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(microseconds=1)
            span = max(0.0, (end_yesterday - yesterday).total_seconds())
            max_steps = int(span // period) + 1
            max_target = yesterday + timedelta(seconds=max_steps * period)
            steps = int((now - yesterday).total_seconds() // period)
            candidate = yesterday + timedelta(seconds=steps * period)
            if candidate > max_target:
                candidate = max_target
        if candidate > now:
            return None
        while until is not None and candidate > until:
            candidate = candidate - timedelta(seconds=period)
        if until is not None and candidate > until:
            return None
        if candidate > now:
            return None
        return candidate

    parsed_times = _parse_times(spec.times or [])
    if not parsed_times:
        return None
    start_day = now.date()
    for offset in range(_SCAN_DAYS):
        current_day = start_day - timedelta(days=offset)
        if not _day_allowed(spec, current_day):
            continue
        for hour, minute in reversed(parsed_times):
            candidate = datetime.combine(
                current_day, time(hour=hour, minute=minute), tzinfo=tz
            )
            if candidate > now:
                continue
            if until is not None and candidate > until:
                continue
            return candidate
    return None


def schedule_cadence(data: Optional[Dict[str, Any]]) -> Tuple[Any, ...]:
    """触发时刻规则的可比身份。次数、到期、补跑、跳过日期不参与，改这些不重置配额。"""
    if not data:
        return ("",)
    try:
        spec = ScheduleSpec.from_dict(data)
    except ScheduleSpecError:
        spec = None
    if spec is None:
        return ("raw", tuple(sorted((str(k), repr(v)) for k, v in data.items())))
    run_at = spec.run_at.isoformat() if spec.run_at is not None else ""
    return (
        spec.kind,
        tuple(spec.times or ()),
        tuple(spec.weekdays or ()),
        tuple(spec.month_days or ()),
        tuple(spec.months or ()),
        int(spec.interval_seconds or 0),
        spec.start_time or "",
        run_at,
    )


def _as_tz(moment: datetime, tz: tzinfo) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


def _parse_times(times: Sequence[str]) -> List[Tuple[int, int]]:
    parsed: List[Tuple[int, int]] = []
    for item in times:
        hm = _parse_hm(item)
        if hm is not None:
            parsed.append(hm)
    return sorted(set(parsed))


def _day_allowed(spec: ScheduleSpec, day: date) -> bool:
    if spec.months and day.month not in spec.months:
        return False
    if spec.month_days and day.day not in spec.month_days:
        return False
    if spec.weekdays and day.isoweekday() not in spec.weekdays:
        return False
    if spec.exceptions and day.isoformat() in spec.exceptions:
        return False
    return True


def remaining_runs(job: Any, spec: Optional[ScheduleSpec] = None) -> Optional[int]:
    """还剩多少次（count 未设置时返回 None 表示不限次数）。"""
    spec = spec or effective_spec(job)
    if spec.count is None:
        return None
    done = int(getattr(job, "runs_done", 0) or 0)
    return max(0, int(spec.count) - done)


def next_fire_at_for_job(job: Any, now: datetime, tz: tzinfo) -> Optional[datetime]:
    """带次数配额的下一次触发时刻（count 用尽即返回 None）。

    时间求解本身仍是纯函数 ``next_fire_at``；这里只叠加 JobDefinition.runs_done 这一份状态。
    """
    spec = effective_spec(job)
    remaining = remaining_runs(job, spec)
    if remaining is not None and remaining <= 0:
        return None
    return next_fire_at(spec, now, tz)


def exhaustion_reason(job: Any, now: datetime, tz: tzinfo) -> Optional[str]:
    """规则无可触发时刻时的原因，供调度器区分「跑完了」和「配置永远不会命中」。"""
    spec = effective_spec(job)
    remaining = remaining_runs(job, spec)
    if remaining is not None and remaining <= 0:
        return f"count 已用尽（共 {spec.count} 次）"
    if spec.until is not None and _as_tz(spec.until, tz) <= _as_tz(now, tz):
        return f"已达 until（{spec.until:%Y-%m-%d %H:%M}）"
    return None


def describe_schedule(spec: Optional[ScheduleSpec]) -> str:
    """供 list_scheduled_jobs / kernel 终端 / dashboard 共用的单行描述。"""
    if spec is None:
        return "unknown"
    try:
        return spec.describe()
    except Exception:  # pragma: no cover - 展示层不应抛错
        return "unknown"


def upcoming_fire_times(
    spec: ScheduleSpec,
    now: datetime,
    tz: tzinfo,
    count: int = 5,
) -> List[datetime]:
    """dry-run 用：连续求解 count 个未来触发时刻（内部把 now 往前推，仍走同一纯函数）。"""
    out: List[datetime] = []
    cursor = _as_tz(now, tz)
    for _ in range(max(0, count)):
        nxt = next_fire_at(spec, cursor, tz)
        if nxt is None:
            break
        out.append(nxt)
        cursor = nxt
    return out
