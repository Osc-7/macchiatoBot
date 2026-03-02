"""
Planner 排程引擎（v1：评分 + 贪心）。
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from schedule_agent.config import PlanningConfig
from schedule_agent.models import Event, EventStatus, SlotType, Task, TimeSlot

from .scoring import rank_tasks
from .types import PlannerPlannedItem, PlannerResult, PlannerUnplannedItem


def _parse_hhmm(value: str) -> tuple[int, int]:
    """解析 HH:MM 格式时间。"""
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: {value}")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid time value: {value}")
    return hour, minute


def _to_naive_local(dt: datetime, timezone: str) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)


def _overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and a_end > b_start


class PlannerEngine:
    """规划引擎：将任务安排到工作时段空档。"""

    def __init__(self, config: PlanningConfig):
        self._config = config

    def _build_working_slots(self, start_date: date, days: int) -> list[TimeSlot]:
        slots: list[TimeSlot] = []
        for offset in range(days):
            current = start_date + timedelta(days=offset)
            weekday = current.isoweekday()
            for window in self._config.working_hours:
                if window.weekday != weekday:
                    continue
                sh, sm = _parse_hhmm(window.start)
                eh, em = _parse_hhmm(window.end)
                start_dt = datetime(current.year, current.month, current.day, sh, sm)
                end_dt = datetime(current.year, current.month, current.day, eh, em)
                if end_dt <= start_dt:
                    continue
                slots.append(
                    TimeSlot(
                        start_time=start_dt,
                        end_time=end_dt,
                        slot_type=SlotType.FREE,
                        title="工作时段",
                    )
                )
        slots.sort(key=lambda slot: slot.start_time)
        return slots

    def _collect_busy_intervals(
        self,
        events: list[Event],
        window_start: datetime,
        window_end: datetime,
    ) -> list[tuple[datetime, datetime]]:
        intervals: list[tuple[datetime, datetime]] = []
        for event in events:
            if event.status == EventStatus.CANCELLED:
                continue
            if not event.is_blocking:
                continue
            start = _to_naive_local(event.start_time, self._config.timezone)
            end = _to_naive_local(event.end_time, self._config.timezone)
            if end <= start:
                continue
            if _overlap(start, end, window_start, window_end):
                intervals.append((max(start, window_start), min(end, window_end)))
        intervals.sort(key=lambda item: item[0])
        return intervals

    def _subtract_interval(self, slot: TimeSlot, busy: tuple[datetime, datetime]) -> list[TimeSlot]:
        b_start, b_end = busy
        s_start = slot.start_time
        s_end = slot.end_time
        if not _overlap(s_start, s_end, b_start, b_end):
            return [slot]

        result: list[TimeSlot] = []
        if s_start < b_start:
            result.append(
                TimeSlot(
                    start_time=s_start,
                    end_time=b_start,
                    slot_type=SlotType.FREE,
                    title=slot.title,
                )
            )
        if b_end < s_end:
            result.append(
                TimeSlot(
                    start_time=b_end,
                    end_time=s_end,
                    slot_type=SlotType.FREE,
                    title=slot.title,
                )
            )
        return result

    def _subtract_busy(self, working_slots: list[TimeSlot], busy_intervals: list[tuple[datetime, datetime]]) -> list[TimeSlot]:
        free_slots = working_slots
        for busy in busy_intervals:
            updated: list[TimeSlot] = []
            for slot in free_slots:
                updated.extend(self._subtract_interval(slot, busy))
            free_slots = updated
        free_slots = [
            slot for slot in free_slots if slot.duration_minutes >= self._config.min_block_minutes
        ]
        free_slots.sort(key=lambda slot: slot.start_time)
        return free_slots

    def _sort_slots_prefer_weekday(self, slots: list[TimeSlot]) -> list[TimeSlot]:
        """优先工作日（1-5）时段，周末（6、7）作为补充。"""
        result = list(slots)
        result.sort(
            key=lambda s: (
                0 if s.start_time.isoweekday() <= 5 else 1,
                s.start_time,
            )
        )
        return result

    def plan(
        self,
        tasks: list[Task],
        events: list[Event],
        start_date: date,
        days: int,
    ) -> PlannerResult:
        if days < 1:
            days = 1

        working_slots = self._build_working_slots(start_date, days)
        if not working_slots:
            return PlannerResult(planned_items=[], unplanned_items=[])

        window_start = working_slots[0].start_time
        window_end = working_slots[-1].end_time

        busy_intervals = self._collect_busy_intervals(events, window_start, window_end)
        free_slots = self._subtract_busy(working_slots, busy_intervals)

        # 周末权重：优先工作日时段
        if self._config.prefer_weekday_slots:
            free_slots = self._sort_slots_prefer_weekday(free_slots)

        scored_tasks = rank_tasks(tasks, start_date, self._config.weights)
        planned_items: list[PlannerPlannedItem] = []
        unplanned_items: list[PlannerUnplannedItem] = []

        break_min = self._config.break_minutes_after_task
        for task, score in scored_tasks:
            required = max(task.estimated_minutes, self._config.min_block_minutes)
            # 休息权重：每个任务占用 任务时长 + 休息时长
            total_required = required + break_min
            chosen_idx = -1
            for idx, slot in enumerate(free_slots):
                if slot.duration_minutes >= total_required:
                    chosen_idx = idx
                    break

            if chosen_idx < 0:
                unplanned_items.append(
                    PlannerUnplannedItem(
                        task_id=task.id,
                        reason="没有足够长的空闲工作时段",
                        score=score,
                    )
                )
                continue

            chosen = free_slots.pop(chosen_idx)
            task_end = chosen.start_time + timedelta(minutes=required)
            # 槽位实际 consumed 到 任务结束 + 休息
            end_time = chosen.start_time + timedelta(minutes=total_required)
            planned_items.append(
                PlannerPlannedItem(
                    task_id=task.id,
                    start_at=chosen.start_time,
                    end_at=task_end,
                    score=score,
                )
            )

            # 回填剩余空档（休息时间后的部分）
            if end_time < chosen.end_time:
                remainder = TimeSlot(
                    start_time=end_time,
                    end_time=chosen.end_time,
                    slot_type=SlotType.FREE,
                    title=chosen.title,
                )
                if remainder.duration_minutes >= self._config.min_block_minutes:
                    free_slots.append(remainder)
                    free_slots.sort(key=lambda slot: slot.start_time)

        return PlannerResult(
            planned_items=planned_items,
            unplanned_items=unplanned_items,
            window_start=window_start,
            window_end=window_end,
        )

