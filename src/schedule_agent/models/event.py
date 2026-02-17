"""
Event 模型 - 表示日程事件

日程事件是具有固定开始和结束时间的活动，例如会议、约会等。
"""

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field, ConfigDict


class EventStatus(str, Enum):
    """事件状态"""
    SCHEDULED = "scheduled"  # 已安排
    IN_PROGRESS = "in_progress"  # 进行中
    COMPLETED = "completed"  # 已完成
    CANCELLED = "cancelled"  # 已取消


class EventPriority(str, Enum):
    """事件优先级"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class Event(BaseModel):
    """
    日程事件模型

    表示一个具有固定开始和结束时间的日程事件。

    Attributes:
        id: 唯一标识符
        title: 事件标题
        description: 事件描述
        start_time: 开始时间
        end_time: 结束时间
        location: 地点
        status: 事件状态
        priority: 优先级
        tags: 标签列表
        reminders: 提醒时间列表（分钟为单位，如 [15, 30] 表示提前15和30分钟提醒）
        created_at: 创建时间
        updated_at: 更新时间
    """

    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    title: str = Field(..., description="事件标题", min_length=1, max_length=200)
    description: Optional[str] = Field(None, description="事件描述", max_length=2000)
    start_time: datetime = Field(..., description="开始时间")
    end_time: datetime = Field(..., description="结束时间")
    location: Optional[str] = Field(None, description="地点", max_length=200)
    status: EventStatus = Field(default=EventStatus.SCHEDULED, description="事件状态")
    priority: EventPriority = Field(default=EventPriority.MEDIUM, description="优先级")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    reminders: list[int] = Field(
        default_factory=list,
        description="提醒时间列表（分钟为单位）"
    )
    created_at: datetime = Field(
        default_factory=datetime.now,
        description="创建时间"
    )
    updated_at: datetime = Field(
        default_factory=datetime.now,
        description="更新时间"
    )

    model_config = ConfigDict(
        json_encoders={datetime: lambda v: v.isoformat()}
    )

    def update_timestamp(self) -> None:
        """更新修改时间"""
        self.updated_at = datetime.now()

    @property
    def duration_minutes(self) -> int:
        """计算事件持续时间（分钟）"""
        delta = self.end_time - self.start_time
        return int(delta.total_seconds() / 60)

    @property
    def is_all_day(self) -> bool:
        """判断是否为全天事件（持续时间>=24小时）"""
        return self.duration_minutes >= 24 * 60

    def is_conflict_with(self, other: "Event") -> bool:
        """
        检查是否与另一个事件时间冲突

        Args:
            other: 另一个事件

        Returns:
            是否存在时间冲突
        """
        # 如果任一事件已取消，则不算冲突
        if self.status == EventStatus.CANCELLED or other.status == EventStatus.CANCELLED:
            return False

        return (
            self.start_time < other.end_time and
            self.end_time > other.start_time
        )

    def __str__(self) -> str:
        """字符串表示"""
        start_str = self.start_time.strftime("%Y-%m-%d %H:%M")
        end_str = self.end_time.strftime("%H:%M")
        return f"[{self.id}] {self.title} ({start_str} - {end_str})"
