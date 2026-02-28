"""Canvas LMS 数据模型定义"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Any


def now_utc() -> datetime:
    """获取当前 UTC 时间（timezone-aware）"""
    return datetime.now(timezone.utc)


@dataclass
class CanvasAssignment:
    """Canvas 作业模型
    
    Attributes:
        id: 作业 ID
        name: 作业名称
        description: 作业描述
        course_id: 课程 ID
        course_name: 课程名称
        due_at: 截止时间
        lock_at: 锁定时间（之后无法提交）
        unlock_at: 解锁时间（之前不可见）
        points_possible: 总分
        submission_types: 允许的提交类型
        is_submitted: 是否已提交
        submitted_at: 提交时间
        workflow_state: 提交状态 (submitted, graded, missing, late)
        grade: 成绩
        attempt: 提交次数
        url: 作业链接
        html_url: Canvas 网页链接
    """
    id: int
    name: str
    description: str = ""
    course_id: int = 0
    course_name: str = ""
    due_at: Optional[datetime] = None
    lock_at: Optional[datetime] = None
    unlock_at: Optional[datetime] = None
    points_possible: float = 0.0
    submission_types: List[str] = field(default_factory=list)
    is_submitted: bool = False
    submitted_at: Optional[datetime] = None
    workflow_state: str = ""  # submitted, graded, missing, late
    grade: Optional[str] = None
    attempt: int = 0
    url: str = ""
    html_url: str = ""
    
    @property
    def days_left(self) -> int:
        """距离截止还有多少天"""
        if not self.due_at:
            return 999
        # 统一使用 timezone-aware 时间比较
        now = now_utc()
        due = self.due_at
        # 如果 due_at 是 naive，假设它是 UTC
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        delta = due - now
        return max(0, delta.days)
    
    @property
    def is_late(self) -> bool:
        """是否已过期"""
        if not self.due_at:
            return False
        now = now_utc()
        due = self.due_at
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        return now > due and not self.is_submitted
    
    @property
    def is_graded(self) -> bool:
        """是否已评分"""
        return self.workflow_state == "graded" and self.grade is not None
    
    @classmethod
    def from_api_response(cls, data: dict, course_name: str = "") -> "CanvasAssignment":
        """从 API 响应创建作业对象
        
        Args:
            data: Canvas API 返回的作业数据
            course_name: 课程名称（需要单独传入）
            
        Returns:
            CanvasAssignment 实例
        """
        # 解析提交信息
        submission = data.get("submission", {}) or {}
        workflow_state = submission.get("workflow_state", "")
        
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            course_id=data["course_id"],
            course_name=course_name,
            due_at=cls._parse_datetime(data.get("due_at")),
            lock_at=cls._parse_datetime(data.get("lock_at")),
            unlock_at=cls._parse_datetime(data.get("unlock_at")),
            points_possible=data.get("points_possible", 0.0) or 0.0,
            submission_types=data.get("submission_types", []),
            is_submitted=workflow_state in ("submitted", "graded"),
            submitted_at=cls._parse_datetime(submission.get("submitted_at")),
            workflow_state=workflow_state,
            grade=submission.get("grade"),
            attempt=submission.get("attempt", 0),
            url=data.get("url", ""),
            html_url=data.get("html_url", ""),
        )
    
    @staticmethod
    def _parse_datetime(date_str: Optional[str]) -> Optional[datetime]:
        """解析 ISO 格式日期字符串，返回 timezone-aware datetime"""
        if not date_str:
            return None
        try:
            # Canvas 返回 ISO 8601 格式，通常带 Z 或 +00:00
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            # 确保返回的是 timezone-aware
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, AttributeError):
            return None
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "course_name": self.course_name,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "points_possible": self.points_possible,
            "is_submitted": self.is_submitted,
            "workflow_state": self.workflow_state,
            "grade": self.grade,
            "days_left": self.days_left,
            "html_url": self.html_url,
        }


@dataclass
class CanvasEvent:
    """Canvas 日历事件模型
    
    Attributes:
        id: 事件 ID
        title: 事件标题
        description: 事件描述
        start_at: 开始时间
        end_at: 结束时间
        course_id: 课程 ID（如果是课程相关事件）
        course_name: 课程名称
        event_type: 事件类型 (Assignment, Event)
        all_day: 是否全天事件
        url: 事件链接
        html_url: Canvas 网页链接
    """
    id: int
    title: str
    description: str = ""
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    course_id: Optional[int] = None
    course_name: str = ""
    event_type: str = ""  # Assignment, Event
    all_day: bool = False
    url: str = ""
    html_url: str = ""
    
    @property
    def duration_hours(self) -> float:
        """事件持续时间（小时）"""
        if not self.start_at or not self.end_at:
            return 2.0  # 默认 2 小时
        delta = self.end_at - self.start_at
        return delta.total_seconds() / 3600
    
    @classmethod
    def from_api_response(cls, data: dict) -> "CanvasEvent":
        """从 API 响应创建事件对象
        
        Args:
            data: Canvas API 返回的事件数据
            
        Returns:
            CanvasEvent 实例
        """
        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            start_at=cls._parse_datetime(data.get("start_at")),
            end_at=cls._parse_datetime(data.get("end_at")),
            course_id=data.get("course_id"),
            course_name=data.get("course_name", ""),
            event_type=data.get("type", ""),
            all_day=data.get("all_day", False),
            url=data.get("url", ""),
            html_url=data.get("html_url", ""),
        )
    
    @staticmethod
    def _parse_datetime(date_str: Optional[str]) -> Optional[datetime]:
        """解析 ISO 格式日期字符串，返回 timezone-aware datetime"""
        if not date_str:
            return None
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, AttributeError):
            return None
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "title": self.title,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "course_name": self.course_name,
            "event_type": self.event_type,
            "all_day": self.all_day,
            "html_url": self.html_url,
        }


@dataclass
class SyncResult:
    """同步结果模型
    
    Attributes:
        created_count: 新建的事件数量
        updated_count: 更新的事件数量
        skipped_count: 跳过的事件数量（已存在）
        errors: 错误列表
        synced_ids: 已同步的事件 ID 列表
    """
    created_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    errors: List[str] = field(default_factory=list)
    synced_ids: List[int] = field(default_factory=list)
    
    @property
    def total_processed(self) -> int:
        """总共处理的事件数量"""
        return self.created_count + self.updated_count + self.skipped_count
    
    @property
    def success_rate(self) -> float:
        """成功率"""
        if self.total_processed == 0:
            return 1.0
        return (self.created_count + self.updated_count) / self.total_processed
    
    def add_error(self, error: str):
        """添加错误记录"""
        self.errors.append(error)
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "created_count": self.created_count,
            "updated_count": self.updated_count,
            "skipped_count": self.skipped_count,
            "total_processed": self.total_processed,
            "success_rate": self.success_rate,
            "errors": self.errors,
        }
    
    def __str__(self) -> str:
        """字符串表示"""
        return (
            f"SyncResult: created={self.created_count}, "
            f"updated={self.updated_count}, skipped={self.skipped_count}, "
            f"errors={len(self.errors)}"
        )
