"""
JSON 存储仓库 - 实现 Event 和 Task 的持久化存储

使用 JSON 文件存储日程数据，支持 CRUD 操作和查询功能。
"""

import json
import os
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, List, Generic, TypeVar, Union
from threading import Lock

from pydantic import BaseModel

from ..models import Event, Task, EventStatus, TaskStatus


T = TypeVar("T", bound=BaseModel)


class JSONRepository(Generic[T]):
    """
    通用 JSON 存储仓库

    提供 CRUD 操作和数据持久化功能。

    Attributes:
        file_path: JSON 文件路径
        model_class: 数据模型类
    """

    def __init__(self, file_path: Union[str, Path], model_class: type[T]):
        """
        初始化存储仓库

        Args:
            file_path: JSON 文件路径
            model_class: 数据模型类
        """
        self.file_path = Path(file_path)
        self.model_class = model_class
        self._lock = Lock()
        self._ensure_file_exists()

    def _ensure_file_exists(self) -> None:
        """确保存储文件存在"""
        if not self.file_path.exists():
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_data({})

    def _read_data(self) -> dict:
        """读取所有数据"""
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_data(self, data: dict) -> None:
        """写入所有数据"""
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=self._json_serializer)

    @staticmethod
    def _json_serializer(obj):
        """JSON 序列化器"""
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, BaseModel):
            return obj.model_dump()
        raise TypeError(f"Type {type(obj)} is not JSON serializable")

    def _item_to_dict(self, item: T) -> dict:
        """将模型实例转换为字典"""
        return item.model_dump(mode="json")

    def _dict_to_item(self, data: dict) -> T:
        """将字典转换为模型实例"""
        return self.model_class.model_validate(data)

    def create(self, item: T) -> T:
        """
        创建新记录

        Args:
            item: 要创建的数据模型实例

        Returns:
            创建的数据模型实例
        """
        with self._lock:
            data = self._read_data()
            data[item.id] = self._item_to_dict(item)
            self._write_data(data)
            return item

    def get(self, id: str) -> Optional[T]:
        """
        根据 ID 获取记录

        Args:
            id: 记录 ID

        Returns:
            找到的记录，如果不存在则返回 None
        """
        data = self._read_data()
        if id in data:
            return self._dict_to_item(data[id])
        return None

    def update(self, item: T) -> Optional[T]:
        """
        更新记录

        Args:
            item: 要更新的数据模型实例

        Returns:
            更新后的记录，如果不存在则返回 None
        """
        with self._lock:
            data = self._read_data()
            if item.id not in data:
                return None
            data[item.id] = self._item_to_dict(item)
            self._write_data(data)
            return item

    def delete(self, id: str) -> bool:
        """
        删除记录

        Args:
            id: 记录 ID

        Returns:
            是否删除成功
        """
        with self._lock:
            data = self._read_data()
            if id not in data:
                return False
            del data[id]
            self._write_data(data)
            return True

    def get_all(self) -> List[T]:
        """
        获取所有记录

        Returns:
            所有记录列表
        """
        data = self._read_data()
        return [self._dict_to_item(item_data) for item_data in data.values()]

    def count(self) -> int:
        """
        获取记录总数

        Returns:
            记录数量
        """
        data = self._read_data()
        return len(data)

    def clear(self) -> None:
        """清空所有记录"""
        with self._lock:
            self._write_data({})


class EventRepository(JSONRepository[Event]):
    """
    Event 专用存储仓库

    提供 Event 相关的查询功能。
    """

    def __init__(self, file_path: Union[str, Path] = None):
        if file_path is None:
            file_path = Path("data/events.json")
        super().__init__(file_path, Event)

    def get_by_date_range(
        self,
        start: datetime,
        end: datetime,
    ) -> List[Event]:
        """
        获取指定时间范围内的事件

        Args:
            start: 开始时间
            end: 结束时间

        Returns:
            匹配的事件列表
        """
        all_events = self.get_all()
        return [
            event for event in all_events
            if event.start_time < end and event.end_time > start
            and event.status != EventStatus.CANCELLED
        ]

    def get_by_date(self, target_date: date) -> List[Event]:
        """
        获取指定日期的事件

        Args:
            target_date: 目标日期

        Returns:
            匹配的事件列表
        """
        start = datetime.combine(target_date, datetime.min.time())
        end = datetime.combine(target_date, datetime.max.time())
        return self.get_by_date_range(start, end)

    def get_today(self) -> List[Event]:
        """
        获取今天的事件

        Returns:
            今天的事件列表
        """
        return self.get_by_date(date.today())

    def get_upcoming(self, days: int = 7) -> List[Event]:
        """
        获取未来几天的事件

        Args:
            days: 天数

        Returns:
            未来事件列表
        """
        start = datetime.now()
        end = start + timedelta(days=days)
        return self.get_by_date_range(start, end)

    def get_by_status(self, status: EventStatus) -> List[Event]:
        """
        获取指定状态的事件

        Args:
            status: 事件状态

        Returns:
            匹配的事件列表
        """
        all_events = self.get_all()
        return [event for event in all_events if event.status == status]

    def find_conflicts(self, event: Event, exclude_id: str = None) -> List[Event]:
        """
        查找与指定事件冲突的事件

        Args:
            event: 要检查的事件
            exclude_id: 排除的事件 ID（用于更新时排除自身）

        Returns:
            冲突的事件列表
        """
        all_events = self.get_all()
        conflicts = []
        for existing in all_events:
            if exclude_id and existing.id == exclude_id:
                continue
            if event.is_conflict_with(existing):
                conflicts.append(existing)
        return conflicts

    def search(self, query: str) -> List[Event]:
        """
        搜索事件

        Args:
            query: 搜索关键词

        Returns:
            匹配的事件列表
        """
        all_events = self.get_all()
        query_lower = query.lower()
        return [
            event for event in all_events
            if query_lower in event.title.lower()
            or (event.description and query_lower in event.description.lower())
            or any(query_lower in tag.lower() for tag in event.tags)
        ]


class TaskRepository(JSONRepository[Task]):
    """
    Task 专用存储仓库

    提供 Task 相关的查询功能。
    """

    def __init__(self, file_path: Union[str, Path] = None):
        if file_path is None:
            file_path = Path("data/tasks.json")
        super().__init__(file_path, Task)

    def get_by_status(self, status: TaskStatus) -> List[Task]:
        """
        获取指定状态的任务

        Args:
            status: 任务状态

        Returns:
            匹配的任务列表
        """
        all_tasks = self.get_all()
        return [task for task in all_tasks if task.status == status]

    def get_todo(self) -> List[Task]:
        """
        获取待办任务

        Returns:
            待办任务列表
        """
        return self.get_by_status(TaskStatus.TODO)

    def get_completed(self) -> List[Task]:
        """
        获取已完成任务

        Returns:
            已完成任务列表
        """
        return self.get_by_status(TaskStatus.COMPLETED)

    def get_overdue(self) -> List[Task]:
        """
        获取过期任务

        Returns:
            过期任务列表
        """
        all_tasks = self.get_all()
        return [task for task in all_tasks if task.is_overdue]

    def get_due_today(self) -> List[Task]:
        """
        获取今天截止的任务

        Returns:
            今天截止的任务列表
        """
        all_tasks = self.get_all()
        today = date.today()
        return [
            task for task in all_tasks
            if task.due_date == today and task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)
        ]

    def get_due_this_week(self) -> List[Task]:
        """
        获取本周截止的任务

        Returns:
            本周截止的任务列表
        """
        all_tasks = self.get_all()
        today = date.today()
        week_end = today + timedelta(days=(7 - today.weekday()))
        return [
            task for task in all_tasks
            if task.due_date and today <= task.due_date <= week_end
            and task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)
        ]

    def get_scheduled(self) -> List[Task]:
        """
        获取已安排的任务

        Returns:
            已安排的任务列表
        """
        all_tasks = self.get_all()
        return [task for task in all_tasks if task.is_scheduled]

    def get_unscheduled(self) -> List[Task]:
        """
        获取未安排的任务

        Returns:
            未安排的任务列表
        """
        all_tasks = self.get_all()
        return [
            task for task in all_tasks
            if not task.is_scheduled and task.status == TaskStatus.TODO
        ]

    def search(self, query: str) -> List[Task]:
        """
        搜索任务

        Args:
            query: 搜索关键词

        Returns:
            匹配的任务列表
        """
        all_tasks = self.get_all()
        query_lower = query.lower()
        return [
            task for task in all_tasks
            if query_lower in task.title.lower()
            or (task.description and query_lower in task.description.lower())
            or any(query_lower in tag.lower() for tag in task.tags)
        ]
