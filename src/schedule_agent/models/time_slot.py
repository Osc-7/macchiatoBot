"""
TimeSlot 模型 - 表示时间段

时间段用于表示一个连续的时间区间，主要用于：
- 表示空闲时间段（供规划器使用）
- 表示忙碌时间段
- 时间冲突检测
"""

from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict


class SlotType(str, Enum):
    """时间段类型"""
    FREE = "free"  # 空闲时间
    BUSY = "busy"  # 忙碌时间（有事件）
    SLEEP = "sleep"  # 睡眠时间
    BUFFER = "buffer"  # 缓冲时间


class TimeSlot(BaseModel):
    """
    时间段模型

    表示一个连续的时间区间，可用于表示空闲、忙碌等状态。

    Attributes:
        start_time: 开始时间
        end_time: 结束时间
        slot_type: 时间段类型
        title: 时间段标题（可选，如 "午休"、"睡眠" 等）
        metadata: 额外元数据
    """

    start_time: datetime = Field(..., description="开始时间")
    end_time: datetime = Field(..., description="结束时间")
    slot_type: SlotType = Field(default=SlotType.FREE, description="时间段类型")
    title: Optional[str] = Field(None, description="时间段标题")
    metadata: dict = Field(default_factory=dict, description="额外元数据")

    model_config = ConfigDict(
        json_encoders={datetime: lambda v: v.isoformat()}
    )

    @property
    def duration_minutes(self) -> int:
        """计算时间段长度（分钟）"""
        delta = self.end_time - self.start_time
        return int(delta.total_seconds() / 60)

    @property
    def duration_hours(self) -> float:
        """计算时间段长度（小时）"""
        return self.duration_minutes / 60

    @property
    def is_free(self) -> bool:
        """是否为空闲时间段"""
        return self.slot_type == SlotType.FREE

    @property
    def is_busy(self) -> bool:
        """是否为忙碌时间段"""
        return self.slot_type == SlotType.BUSY

    def contains(self, dt: datetime) -> bool:
        """
        检查指定时间是否在此时间段内

        Args:
            dt: 要检查的时间

        Returns:
            是否在此时间段内
        """
        return self.start_time <= dt < self.end_time

    def overlaps_with(self, other: "TimeSlot") -> bool:
        """
        检查是否与另一个时间段重叠

        Args:
            other: 另一个时间段

        Returns:
            是否存在重叠
        """
        return (
            self.start_time < other.end_time and
            self.end_time > other.start_time
        )

    def can_fit(self, minutes: int) -> bool:
        """
        检查是否可以容纳指定时长

        Args:
            minutes: 所需时长（分钟）

        Returns:
            是否可以容纳
        """
        return self.is_free and self.duration_minutes >= minutes

    def split_for_task(self, minutes: int) -> tuple["TimeSlot", Optional["TimeSlot"]]:
        """
        将时间段分割以容纳指定时长的任务

        Args:
            minutes: 任务所需时长（分钟）

        Returns:
            元组：(分配给任务的时间段, 剩余的空闲时间段)
            如果剩余时间不足以构成有效时间段，则返回 None
        """
        if not self.can_fit(minutes):
            return self, None

        task_end = self.start_time + timedelta(minutes=minutes)

        task_slot = TimeSlot(
            start_time=self.start_time,
            end_time=task_end,
            slot_type=SlotType.BUSY,
            title=self.title
        )

        remaining_minutes = self.duration_minutes - minutes
        if remaining_minutes > 0:
            remaining_slot = TimeSlot(
                start_time=task_end,
                end_time=self.end_time,
                slot_type=SlotType.FREE
            )
        else:
            remaining_slot = None

        return task_slot, remaining_slot

    def intersect(self, other: "TimeSlot") -> Optional["TimeSlot"]:
        """
        计算与另一个时间段的交集

        Args:
            other: 另一个时间段

        Returns:
            交集时间段，如果没有交集则返回 None
        """
        if not self.overlaps_with(other):
            return None

        return TimeSlot(
            start_time=max(self.start_time, other.start_time),
            end_time=min(self.end_time, other.end_time),
            slot_type=self.slot_type
        )

    def merge(self, other: "TimeSlot") -> Optional["TimeSlot"]:
        """
        合并相邻或重叠的时间段

        Args:
            other: 另一个时间段

        Returns:
            合并后的时间段，如果不能合并则返回 None
        """
        # 只有相同类型且相邻或重叠的时间段才能合并
        if self.slot_type != other.slot_type:
            return None

        if not self.overlaps_with(other) and self.end_time != other.start_time and other.end_time != self.start_time:
            return None

        return TimeSlot(
            start_time=min(self.start_time, other.start_time),
            end_time=max(self.end_time, other.end_time),
            slot_type=self.slot_type,
            title=self.title or other.title
        )

    def __str__(self) -> str:
        """字符串表示"""
        start_str = self.start_time.strftime("%Y-%m-%d %H:%M")
        end_str = self.end_time.strftime("%H:%M")
        type_str = self.slot_type.value
        return f"[{type_str}] {start_str} - {end_str} ({self.duration_minutes}分钟)"


def create_sleep_slots(
    date_start: datetime,
    sleep_start_hour: int = 23,
    sleep_start_minute: int = 0,
    sleep_end_hour: int = 8,
    sleep_end_minute: int = 0
) -> list[TimeSlot]:
    """
    为指定日期创建睡眠时间段

    Args:
        date_start: 起始日期（只使用日期部分）
        sleep_start_hour: 睡眠开始小时
        sleep_start_minute: 睡眠开始分钟
        sleep_end_hour: 睡眠结束小时
        sleep_end_minute: 睡眠结束分钟

    Returns:
        睡眠时间段列表
    """
    # 当天晚上的睡眠时间
    evening_sleep_start = date_start.replace(
        hour=sleep_start_hour,
        minute=sleep_start_minute,
        second=0,
        microsecond=0
    )
    # 第二天早上的醒来时间
    morning_wake = (date_start + timedelta(days=1)).replace(
        hour=sleep_end_hour,
        minute=sleep_end_minute,
        second=0,
        microsecond=0
    )

    return [
        TimeSlot(
            start_time=evening_sleep_start,
            end_time=morning_wake,
            slot_type=SlotType.SLEEP,
            title="睡眠时间"
        )
    ]
