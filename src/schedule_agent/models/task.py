"""
Task 模型 - 表示待办任务

任务是待完成的工作项，可以设置预计时长、截止日期，但没有固定的开始时间。
Agent 可以根据任务属性自动规划到合适的时间段。
"""

from datetime import datetime, date
from enum import Enum
from typing import Optional, Literal, Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, ConfigDict


class TaskStatus(str, Enum):
    """任务状态"""
    TODO = "todo"  # 待办
    IN_PROGRESS = "in_progress"  # 进行中
    COMPLETED = "completed"  # 已完成
    CANCELLED = "cancelled"  # 已取消


class TaskPriority(str, Enum):
    """任务优先级"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class Task(BaseModel):
    """
    待办任务模型

    表示一个待完成的任务，具有预计时长和截止日期。

    Attributes:
        id: 唯一标识符
        title: 任务标题
        description: 任务描述
        estimated_minutes: 预计所需时间（分钟）
        due_date: 截止日期
        status: 任务状态
        priority: 优先级
        tags: 标签列表
        scheduled_start: 已安排的开始时间（由规划器设置）
        scheduled_end: 已安排的结束时间（由规划器设置）
        created_at: 创建时间
        updated_at: 更新时间
        completed_at: 完成时间
    """

    id: str = Field(default_factory=lambda: str(uuid4())[:8])
    title: str = Field(..., description="任务标题", min_length=1, max_length=200)
    description: Optional[str] = Field(None, description="任务描述", max_length=2000)
    estimated_minutes: int = Field(
        default=60,
        description="预计所需时间（分钟）",
        ge=1,
        le=1440  # 最大 24 小时
    )
    due_date: Optional[date] = Field(None, description="截止日期")
    status: TaskStatus = Field(default=TaskStatus.TODO, description="任务状态")
    priority: TaskPriority = Field(default=TaskPriority.MEDIUM, description="优先级")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    difficulty: int = Field(
        default=3,
        ge=1,
        le=5,
        description="任务难度评分（1-5）",
    )
    importance: int = Field(
        default=3,
        ge=1,
        le=5,
        description="用户重视程度（1-5）",
    )
    source: Literal["user", "canvas", "planner", "system"] = Field(
        default="user",
        description="任务来源",
    )
    origin_ref: Optional[str] = Field(
        default=None,
        description="外部来源引用 ID（如 canvas assignment id）",
        max_length=200,
    )
    deadline_event_id: Optional[str] = Field(
        default=None,
        description="关联截止事件 ID",
        max_length=64,
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="附加元数据",
    )
    scheduled_start: Optional[datetime] = Field(None, description="已安排的开始时间")
    scheduled_end: Optional[datetime] = Field(None, description="已安排的结束时间")
    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")
    updated_at: datetime = Field(default_factory=datetime.now, description="更新时间")
    completed_at: Optional[datetime] = Field(None, description="完成时间")

    model_config = ConfigDict(
        json_encoders={
            datetime: lambda v: v.isoformat() if v else None,
            date: lambda v: v.isoformat() if v else None
        }
    )

    @field_validator("due_date", mode="before")
    @classmethod
    def parse_due_date(cls, v):
        """支持字符串格式的日期解析"""
        if isinstance(v, str):
            return datetime.strptime(v, "%Y-%m-%d").date()
        return v

    def update_timestamp(self) -> None:
        """更新修改时间"""
        self.updated_at = datetime.now()

    def mark_completed(self) -> None:
        """标记任务为已完成"""
        self.status = TaskStatus.COMPLETED
        self.completed_at = datetime.now()
        self.update_timestamp()

    def mark_cancelled(self) -> None:
        """标记任务为已取消"""
        self.status = TaskStatus.CANCELLED
        self.update_timestamp()

    @property
    def is_scheduled(self) -> bool:
        """任务是否已被安排"""
        return self.scheduled_start is not None and self.scheduled_end is not None

    @property
    def is_overdue(self) -> bool:
        """任务是否已过期"""
        if self.due_date is None:
            return False
        if self.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            return False
        return date.today() > self.due_date

    @property
    def is_completed(self) -> bool:
        """任务是否已完成"""
        return self.status == TaskStatus.COMPLETED

    @property
    def estimated_hours(self) -> float:
        """预计所需时间（小时）"""
        return self.estimated_minutes / 60

    def schedule(self, start: datetime, end: datetime) -> None:
        """
        安排任务到指定时间段

        Args:
            start: 开始时间
            end: 结束时间
        """
        self.scheduled_start = start
        self.scheduled_end = end
        self.status = TaskStatus.IN_PROGRESS
        self.update_timestamp()

    def unschedule(self) -> None:
        """取消任务的时间安排"""
        self.scheduled_start = None
        self.scheduled_end = None
        if self.status == TaskStatus.IN_PROGRESS:
            self.status = TaskStatus.TODO
        self.update_timestamp()

    def __str__(self) -> str:
        """字符串表示"""
        status_emoji = {
            TaskStatus.TODO: "📋",
            TaskStatus.IN_PROGRESS: "🔄",
            TaskStatus.COMPLETED: "✅",
            TaskStatus.CANCELLED: "❌"
        }
        emoji = status_emoji.get(self.status, "📋")
        due_str = f" (截止: {self.due_date})" if self.due_date else ""
        return f"{emoji} [{self.id}] {self.title} - {self.estimated_minutes}分钟{due_str}"
