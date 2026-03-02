"""
Canvas 同步工具。

将 Canvas 课程作业/日历事件同步为本地任务与日程。
"""

import os
from datetime import datetime
from typing import Optional

from canvas_integration import CanvasClient, CanvasConfig, CanvasSync
from schedule_agent.config import Config
from schedule_agent.models import Event, EventPriority, EventStatus, Task, TaskPriority, TaskStatus
from schedule_agent.storage.json_repository import EventRepository, TaskRepository

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_priority(priority: str) -> tuple[EventPriority, TaskPriority]:
    try:
        event_priority = EventPriority(priority)
    except ValueError:
        event_priority = EventPriority.MEDIUM
    try:
        task_priority = TaskPriority(priority)
    except ValueError:
        task_priority = TaskPriority.MEDIUM
    return event_priority, task_priority


class SyncCanvasTool(BaseTool):
    """同步 Canvas 数据到本地日程/任务。"""

    def __init__(
        self,
        config: Optional[Config] = None,
        event_repository: Optional[EventRepository] = None,
        task_repository: Optional[TaskRepository] = None,
    ):
        self._config = config
        self._event_repository = event_repository or EventRepository()
        self._task_repository = task_repository or TaskRepository()
        self._write_tasks = True
        self._write_deadline_events = True

    @property
    def name(self) -> str:
        return "sync_canvas"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="sync_canvas",
            description="""从 Canvas 拉取作业和课程事件，并同步到本地日程。

适用场景：
- 用户要求“同步 Canvas 作业/课程安排”
- 用户希望把近期截止作业自动加入任务和日程

工具会自动：
- 调用 Canvas API 拉取未来 N 天数据
- 作业生成 Task（可被 planner 排程）
- 作业截止时间生成 deadline 事件
- 返回同步统计（创建/跳过/错误）
""",
            parameters=[
                ToolParameter(
                    name="days_ahead",
                    type="integer",
                    description="可选，同步未来多少天的数据；默认使用配置值",
                    required=False,
                ),
                ToolParameter(
                    name="include_submitted",
                    type="boolean",
                    description="可选，是否包含已提交作业；默认使用配置值",
                    required=False,
                ),
                ToolParameter(
                    name="write_tasks",
                    type="boolean",
                    description="是否写入任务（默认 true）",
                    required=False,
                ),
                ToolParameter(
                    name="write_deadline_events",
                    type="boolean",
                    description="是否写入截止事件（默认 true）",
                    required=False,
                ),
            ],
            usage_notes=[
                "需要配置 Canvas API Key（config.canvas.api_key 或环境变量 CANVAS_API_KEY）",
                "作业默认会同时生成任务与截止事件，保证可规划+可提醒",
            ],
            tags=["canvas", "同步", "日程", "任务"],
        )

    def _build_canvas_config(self) -> Optional[CanvasConfig]:
        cfg = self._config.canvas if self._config else None
        api_key = (
            (cfg.api_key if cfg and cfg.api_key else None)
            or os.getenv("CANVAS_API_KEY")
        )
        if not api_key:
            return None

        base_url = (cfg.base_url if cfg else None) or os.getenv(
            "CANVAS_BASE_URL", "https://oc.sjtu.edu.cn/api/v1"
        )
        return CanvasConfig(api_key=api_key, base_url=base_url)

    @staticmethod
    def _build_origin_ref(event_data: dict) -> str:
        metadata = event_data.get("metadata") or {}
        canvas_id = metadata.get("canvas_id")
        item_type = metadata.get("type") or "item"
        if canvas_id is not None:
            return f"canvas:{item_type}:{canvas_id}"
        title = event_data.get("title", "unknown")
        start = event_data.get("start_time", "")
        return f"canvas:{item_type}:{title}:{start}"

    def _find_task_by_origin(self, origin_ref: str) -> Optional[Task]:
        for task in self._task_repository.get_all():
            if task.source == "canvas" and task.origin_ref == origin_ref:
                return task
        return None

    def _find_event_by_origin(self, origin_ref: str, event_type: str) -> Optional[Event]:
        for event in self._event_repository.get_all():
            if (
                event.source == "canvas"
                and event.origin_ref == origin_ref
                and event.event_type == event_type
            ):
                return event
        return None

    def _upsert_task_from_assignment(self, event_data: dict, origin_ref: str) -> Task:
        start_dt = _parse_iso_datetime(event_data.get("start_time"))
        end_dt = _parse_iso_datetime(event_data.get("end_time"))

        estimated_minutes = 60
        if start_dt and end_dt and end_dt > start_dt:
            estimated_minutes = max(15, int((end_dt - start_dt).total_seconds() / 60))

        event_priority, task_priority = _normalize_priority(event_data.get("priority", "medium"))
        tags = event_data.get("tags") or []
        metadata = event_data.get("metadata") or {}

        task = self._find_task_by_origin(origin_ref)
        if task is None:
            task = Task(
                title=event_data.get("title", "Canvas 作业"),
                description=event_data.get("description"),
                estimated_minutes=estimated_minutes,
                due_date=end_dt.date() if end_dt else None,
                priority=task_priority,
                tags=tags,
                source="canvas",
                origin_ref=origin_ref,
                metadata=metadata,
            )
            self._task_repository.create(task)
        else:
            task.title = event_data.get("title", task.title)
            task.description = event_data.get("description")
            task.estimated_minutes = estimated_minutes
            task.due_date = end_dt.date() if end_dt else task.due_date
            task.priority = task_priority
            task.tags = tags
            task.metadata = metadata
            task.update_timestamp()
            self._task_repository.update(task)

        if "已提交" in tags and task.status != TaskStatus.COMPLETED:
            task.mark_completed()
            self._task_repository.update(task)

        return task

    def _upsert_deadline_event(self, event_data: dict, origin_ref: str, linked_task_id: Optional[str]) -> Optional[str]:
        start_dt = _parse_iso_datetime(event_data.get("start_time"))
        end_dt = _parse_iso_datetime(event_data.get("end_time"))
        if not start_dt or not end_dt or end_dt <= start_dt:
            return None

        event_priority, _ = _normalize_priority(event_data.get("priority", "medium"))
        tags = event_data.get("tags") or []
        metadata = event_data.get("metadata") or {}

        event = self._find_event_by_origin(origin_ref, "deadline")
        if event is None:
            event = Event(
                title=event_data.get("title", "Canvas 截止"),
                description=event_data.get("description"),
                start_time=start_dt,
                end_time=end_dt,
                priority=event_priority,
                tags=tags,
                source="canvas",
                event_type="deadline",
                is_blocking=True,
                origin_ref=origin_ref,
                linked_task_id=linked_task_id,
                metadata=metadata,
            )
            self._event_repository.create(event)
        else:
            event.title = event_data.get("title", event.title)
            event.description = event_data.get("description")
            event.start_time = start_dt
            event.end_time = end_dt
            event.priority = event_priority
            event.tags = tags
            event.linked_task_id = linked_task_id
            event.metadata = metadata
            event.update_timestamp()
            self._event_repository.update(event)

        if "已提交" in tags and event.status != EventStatus.COMPLETED:
            event.status = EventStatus.COMPLETED
            event.update_timestamp()
            self._event_repository.update(event)

        return event.id

    def _upsert_normal_event(self, event_data: dict, origin_ref: str) -> Optional[str]:
        start_dt = _parse_iso_datetime(event_data.get("start_time"))
        end_dt = _parse_iso_datetime(event_data.get("end_time"))
        if not start_dt or not end_dt or end_dt <= start_dt:
            return None

        event_priority, _ = _normalize_priority(event_data.get("priority", "medium"))
        tags = event_data.get("tags") or []
        metadata = event_data.get("metadata") or {}

        event = self._find_event_by_origin(origin_ref, "normal")
        if event is None:
            event = Event(
                title=event_data.get("title", "Canvas 事件"),
                description=event_data.get("description"),
                start_time=start_dt,
                end_time=end_dt,
                priority=event_priority,
                tags=tags,
                source="canvas",
                event_type="normal",
                is_blocking=True,
                origin_ref=origin_ref,
                metadata=metadata,
            )
            self._event_repository.create(event)
        else:
            event.title = event_data.get("title", event.title)
            event.description = event_data.get("description")
            event.start_time = start_dt
            event.end_time = end_dt
            event.priority = event_priority
            event.tags = tags
            event.metadata = metadata
            event.update_timestamp()
            self._event_repository.update(event)

        return event.id

    async def _create_schedule_event(self, event_data: dict) -> Optional[str]:
        metadata = event_data.get("metadata") or {}
        item_type = metadata.get("type", "event")
        origin_ref = self._build_origin_ref(event_data)

        if item_type == "assignment":
            linked_task_id = None
            if self._write_tasks:
                task = self._upsert_task_from_assignment(event_data, origin_ref)
                linked_task_id = task.id
            if self._write_deadline_events:
                deadline_event_id = self._upsert_deadline_event(event_data, origin_ref, linked_task_id)
                if linked_task_id and deadline_event_id:
                    task = self._task_repository.get(linked_task_id)
                    if task:
                        task.deadline_event_id = deadline_event_id
                        task.update_timestamp()
                        self._task_repository.update(task)
                return deadline_event_id
            return linked_task_id

        return self._upsert_normal_event(event_data, origin_ref)

    async def execute(self, **kwargs) -> ToolResult:
        if self._config and not self._config.canvas.enabled:
            return ToolResult(
                success=False,
                error="CANVAS_DISABLED",
                message="Canvas 工具已注册但当前处于禁用状态，请在 config.yaml 中设置 canvas.enabled=true",
            )

        canvas_config = self._build_canvas_config()
        if canvas_config is None:
            return ToolResult(
                success=False,
                error="CANVAS_API_KEY_MISSING",
                message="未配置 Canvas API Key，请设置 config.canvas.api_key 或环境变量 CANVAS_API_KEY",
            )

        if not canvas_config.validate():
            return ToolResult(
                success=False,
                error="CANVAS_CONFIG_INVALID",
                message="Canvas 配置无效，请检查 base_url 和 api_key",
            )

        default_days = (
            self._config.canvas.default_days_ahead if self._config else 60
        )
        default_include_submitted = (
            self._config.canvas.include_submitted if self._config else False
        )
        days_ahead = int(kwargs.get("days_ahead", default_days))
        include_submitted = bool(
            kwargs.get("include_submitted", default_include_submitted)
        )

        self._write_tasks = bool(kwargs.get("write_tasks", True))
        self._write_deadline_events = bool(kwargs.get("write_deadline_events", True))

        try:
            async with CanvasClient(canvas_config) as client:
                syncer = CanvasSync(
                    client=client,
                    event_creator=self._create_schedule_event,
                )
                sync_result = await syncer.sync_to_schedule(
                    days_ahead=days_ahead,
                    include_submitted=include_submitted,
                )
        except Exception as e:
            return ToolResult(
                success=False,
                error="CANVAS_SYNC_FAILED",
                message=f"Canvas 同步失败: {e}",
            )

        return ToolResult(
            success=len(sync_result.errors) == 0,
            message=(
                f"Canvas 同步完成：创建 {sync_result.created_count}，"
                f"跳过 {sync_result.skipped_count}，错误 {len(sync_result.errors)}"
            ),
            data=sync_result.to_dict(),
            metadata={
                "source": "canvas",
                "write_tasks": self._write_tasks,
                "write_deadline_events": self._write_deadline_events,
            },
        )
