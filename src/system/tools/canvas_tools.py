"""
Canvas 工具集。

- `sync_canvas`: 将 Canvas 课程作业/日历事件同步为本地任务与日程。
- `fetch_canvas_overview`: 只读抓取 Canvas 当前用户概览（课程、作业、事件、Planner 待办）。
- `fetch_canvas_course_content`: 只读查看某门课的大纲与文件列表。
"""

import os
from datetime import datetime
from typing import Optional, List

from frontend.canvas_integration import CanvasClient, CanvasConfig, CanvasSync
from agent_core.config import Config
from agent_core.models import (
    Event,
    EventPriority,
    EventStatus,
    Task,
    TaskPriority,
    TaskStatus,
)
from agent_core.storage.json_repository import EventRepository, TaskRepository

from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult


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
- 当 include_submitted=true 时，已提交的作业仍会拉取；同步逻辑会给任务/事件打上「已提交」并标为 completed
- 返回同步统计（创建/跳过/错误）

重要：若要在本地反映「已在 Canvas 提交」，必须传 include_submitted=true（或把 config.canvas.include_submitted 设为 true）。为 false 时客户端会丢弃已提交作业，本地无法据此更新完成状态。
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
                    description="可选，是否拉取已提交的作业（默认 config.canvas.include_submitted）。为 true 时才会同步已提交状态并写入「已提交」、将对应任务与 deadline 标为 completed；为 false 时未来窗口内已提交项会被忽略。",
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
        api_key = (cfg.api_key if cfg and cfg.api_key else None) or os.getenv(
            "CANVAS_API_KEY"
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

    def _find_event_by_origin(
        self, origin_ref: str, event_type: str
    ) -> Optional[Event]:
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

        event_priority, task_priority = _normalize_priority(
            event_data.get("priority", "medium")
        )
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

    def _upsert_deadline_event(
        self, event_data: dict, origin_ref: str, linked_task_id: Optional[str]
    ) -> Optional[str]:
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
                deadline_event_id = self._upsert_deadline_event(
                    event_data, origin_ref, linked_task_id
                )
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

        default_days = self._config.canvas.default_days_ahead if self._config else 60
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


class FetchCanvasOverviewTool(BaseTool):
    """只读抓取 Canvas 概览数据（不写入本地存储）。"""

    def __init__(self, config: Optional[Config] = None):
        self._config = config

    @property
    def name(self) -> str:
        return "fetch_canvas_overview"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="fetch_canvas_overview",
            description="""从 Canvas 读取当前用户的课程与待办概览（只读，不写入本地）。

适用场景：
- 想先“看看” Canvas 里有哪些课程/作业/事件，再决定如何处理
- 需要一份当前学习负载总览，供 Agent 做总结、梳理或建议

工具会：
- 获取当前用户基本信息
- 获取所有活跃课程列表
- 获取未来 N 天内的作业与日历事件（作业列表与 sync_canvas 同源）
- 获取同一时间窗口内的 Planner 待办/机会项

upcoming_assignments 与 include_submitted：为 false（默认）时仅含未提交作业；为 true 时含未提交与已提交，每条有 is_submitted 字段区分。这不是“只读已提交列表”，而是完整列表。

不会：
- 创建/修改 Canvas 中的任何内容
- 直接写入本地日程或任务，只返回结构化数据""",
            parameters=[
                ToolParameter(
                    name="days_ahead",
                    type="integer",
                    description="可选，概览窗口：未来多少天内的作业/事件/Planner 待办，默认 30 天",
                    required=False,
                ),
                ToolParameter(
                    name="include_submitted",
                    type="boolean",
                    description="可选，与 sync_canvas 一致：false 时 upcoming_assignments 不含已提交；true 时含已提交且可凭 is_submitted 区分。",
                    required=False,
                ),
            ],
            usage_notes=[
                "需要配置 Canvas API Key（config.canvas.api_key 或环境变量 CANVAS_API_KEY）",
                "返回的数据字段设计为便于 Agent 进行自然语言总结与排序",
                "如果只想同步到本地日程，请优先使用 sync_canvas 工具",
            ],
            tags=["canvas", "查询", "只读", "概览"],
        )

    def _build_canvas_config(self) -> Optional[CanvasConfig]:
        cfg = self._config.canvas if self._config else None
        api_key = (cfg.api_key if cfg and cfg.api_key else None) or os.getenv(
            "CANVAS_API_KEY"
        )
        if not api_key:
            return None

        base_url = (cfg.base_url if cfg else None) or os.getenv(
            "CANVAS_BASE_URL", "https://oc.sjtu.edu.cn/api/v1"
        )
        return CanvasConfig(api_key=api_key, base_url=base_url)

    async def execute(self, **kwargs) -> ToolResult:
        # 与 SyncCanvasTool 一致：全局 Canvas 开关优先
        if self._config and not self._config.canvas.enabled:
            return ToolResult(
                success=False,
                error="CANVAS_DISABLED",
                message="Canvas 工具当前处于禁用状态，请在 config.yaml 中设置 canvas.enabled=true",
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

        default_days = self._config.canvas.default_days_ahead if self._config else 30
        default_include_submitted = (
            self._config.canvas.include_submitted if self._config else False
        )
        days_ahead = int(kwargs.get("days_ahead", default_days))
        include_submitted = bool(
            kwargs.get("include_submitted", default_include_submitted)
        )

        overview: dict = {}
        errors: list[str] = []

        try:
            async with CanvasClient(canvas_config) as client:
                # 用户信息
                try:
                    profile = await client.get_user_profile()
                    overview["profile"] = profile
                except Exception as e:
                    errors.append(f"获取用户信息失败: {e}")

                # 课程列表
                try:
                    courses = await client.get_courses()
                    overview["courses"] = courses
                except Exception as e:
                    errors.append(f"获取课程列表失败: {e}")

                # 未来作业
                try:
                    assignments = await client.get_upcoming_assignments(
                        days=days_ahead,
                        include_submitted=include_submitted,
                    )
                    overview["upcoming_assignments"] = [
                        a.to_dict() for a in assignments
                    ]
                except Exception as e:
                    errors.append(f"获取作业失败: {e}")

                # 未来日历事件
                try:
                    events = await client.get_upcoming_events(days=days_ahead)
                    overview["upcoming_events"] = [e.to_dict() for e in events]
                except Exception as e:
                    errors.append(f"获取日历事件失败: {e}")

                # Planner 待办/机会项（默认抓取未完成条目）
                try:
                    planner_items = await client.get_planner_items(
                        filter="incomplete_items"
                    )
                    overview["planner_items"] = [
                        item.to_dict() for item in planner_items
                    ]
                except Exception as e:
                    errors.append(f"获取 Planner 待办失败: {e}")

        except Exception as e:
            return ToolResult(
                success=False,
                error="CANVAS_FETCH_FAILED",
                message=f"Canvas 概览抓取失败: {e}",
            )

        success = len(errors) == 0
        message = (
            f"Canvas 概览抓取完成："
            f"课程 {len(overview.get('courses', []))} 门，"
            f"未来 {days_ahead} 天作业 {len(overview.get('upcoming_assignments', []))} 个，"
            f"事件 {len(overview.get('upcoming_events', []))} 个，"
            f"Planner 待办 {len(overview.get('planner_items', []))} 条。"
        )
        if errors:
            message += " 部分子请求失败：" + "；".join(errors)

        return ToolResult(
            success=success,
            message=message,
            data={
                "overview": overview,
                "errors": errors,
                "days_ahead": days_ahead,
                "include_submitted": include_submitted,
            },
            metadata={
                "source": "canvas",
                "type": "overview",
            },
        )


class FetchCanvasCourseContentTool(BaseTool):
    """按课程查看大纲与文件（只读，不写入本地）。"""

    def __init__(self, config: Optional[Config] = None):
        self._config = config

    @property
    def name(self) -> str:
        return "fetch_canvas_course_content"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="fetch_canvas_course_content",
            description="""按课程查看 Canvas 大纲与文件列表（只读）。

适用场景：
- 用户说“看看 XX 这门课的大纲/课件”；
- 想先列出某门课的 PDF/作业说明文件，再决定要读哪一个。

工具会：
- 根据 course_id 或课程名/课程代码模糊匹配出一门课程；
- 可选返回课程详情（含 syllabus_body 大纲）；
- 可选返回课程文件列表（可支持按关键字/文件类型过滤）。""",
            parameters=[
                ToolParameter(
                    name="course_id",
                    type="integer",
                    description="可选，Canvas 课程 ID；若未提供则通过 course_search 进行模糊匹配",
                    required=False,
                ),
                ToolParameter(
                    name="course_search",
                    type="string",
                    description="可选，用于按课程名或课程代码模糊搜索（如 “代数” 或 “SE101”）",
                    required=False,
                ),
                ToolParameter(
                    name="include_syllabus",
                    type="boolean",
                    description="是否返回课程大纲 syllabus_body，默认 true",
                    required=False,
                ),
                ToolParameter(
                    name="include_files",
                    type="boolean",
                    description="是否返回课程文件列表，默认 true",
                    required=False,
                ),
                ToolParameter(
                    name="file_search_term",
                    type="string",
                    description="可选，仅在 include_files=true 时生效，用于按文件名关键字过滤（如 “HW1” 或 “slides”）",
                    required=False,
                ),
                ToolParameter(
                    name="file_content_types",
                    type="string",
                    description="可选，仅在 include_files=true 时生效；用逗号分隔的 MIME 前缀或简写（如 'pdf,docx'）",
                    required=False,
                ),
            ],
            usage_notes=[
                "若同时传入 course_id 和 course_search，则优先使用 course_id。",
                "course_search 会在课程名和 course_code 上做大小写不敏感包含匹配，若多门课程命中，会在返回的 match_info.ambiguous_courses 中列出。",
                "file_content_types 简写会自动映射常见类型：pdf -> application/pdf, pptx -> application/vnd.openxmlformats-officedocument.presentationml.presentation 等。",
            ],
            tags=["canvas", "课程", "大纲", "文件", "只读"],
        )

    def _build_canvas_config(self) -> Optional[CanvasConfig]:
        cfg = self._config.canvas if self._config else None
        api_key = (cfg.api_key if cfg and cfg.api_key else None) or os.getenv(
            "CANVAS_API_KEY"
        )
        if not api_key:
            return None

        base_url = (cfg.base_url if cfg else None) or os.getenv(
            "CANVAS_BASE_URL", "https://oc.sjtu.edu.cn/api/v1"
        )
        return CanvasConfig(api_key=api_key, base_url=base_url)

    async def _resolve_course_id(
        self,
        client: CanvasClient,
        course_id: Optional[int],
        course_search: Optional[str],
    ) -> tuple[Optional[int], dict]:
        """通过 course_id 或模糊搜索定位课程，并返回匹配信息。"""
        match_info: dict = {}

        if course_id is not None:
            match_info["via"] = "id"
            match_info["course_id"] = course_id
            return course_id, match_info

        if not course_search:
            return None, match_info

        query = course_search.strip().lower()
        if not query:
            return None, match_info

        courses = await client.get_courses()
        candidates: List[dict] = []
        for c in courses:
            name = (c.get("name") or "").lower()
            code = (c.get("course_code") or "").lower()
            if query in name or query in code:
                candidates.append(c)

        if not candidates:
            match_info["via"] = "search"
            match_info["query"] = course_search
            match_info["matched"] = []
            return None, match_info

        # 简单策略：若只有一个候选，则直接选；否则返回全部供上层解释
        chosen = candidates[0]
        match_info["via"] = "search"
        match_info["query"] = course_search
        match_info["matched"] = [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "course_code": c.get("course_code"),
            }
            for c in candidates
        ]
        return int(chosen["id"]), match_info

    def _normalize_content_types(self, raw: Optional[str]) -> List[str]:
        """将逗号分隔的简写转换为 MIME 类型列表。"""
        if not raw:
            return []
        parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
        mime_map = {
            "pdf": "application/pdf",
            "doc": "application/msword",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "ppt": "application/vnd.ms-powerpoint",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "xls": "application/vnd.ms-excel",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        result: List[str] = []
        for p in parts:
            if p in mime_map:
                result.append(mime_map[p])
            elif "/" in p:
                result.append(p)
        return result

    async def execute(self, **kwargs) -> ToolResult:
        if self._config and not self._config.canvas.enabled:
            return ToolResult(
                success=False,
                error="CANVAS_DISABLED",
                message="Canvas 工具当前处于禁用状态，请在 config.yaml 中设置 canvas.enabled=true",
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

        include_syllabus = bool(kwargs.get("include_syllabus", True))
        include_files = bool(kwargs.get("include_files", True))
        course_id = kwargs.get("course_id")
        course_search = kwargs.get("course_search")
        file_search_term = kwargs.get("file_search_term")
        file_content_types_str = kwargs.get("file_content_types")
        file_content_types = self._normalize_content_types(file_content_types_str)

        overview: dict = {}
        match_info: dict = {}

        try:
            async with CanvasClient(canvas_config) as client:
                resolved_id, match_info = await self._resolve_course_id(
                    client,
                    course_id=course_id,
                    course_search=course_search,
                )
                if resolved_id is None:
                    return ToolResult(
                        success=False,
                        error="COURSE_NOT_FOUND",
                        message="未能根据给定的 course_id / course_search 找到课程，请调整搜索条件。",
                        data={"match_info": match_info},
                    )

                # 课程详情（含大纲）
                if include_syllabus:
                    try:
                        course = await client.get_course(
                            resolved_id,
                            include_syllabus=True,
                        )
                        overview["course"] = {
                            "id": course.get("id"),
                            "name": course.get("name"),
                            "course_code": course.get("course_code"),
                            "syllabus_body": course.get("syllabus_body"),
                            "start_at": course.get("start_at"),
                            "end_at": course.get("end_at"),
                            "html_url": course.get("html_url"),
                        }
                    except Exception as e:
                        return ToolResult(
                            success=False,
                            error="COURSE_FETCH_FAILED",
                            message=f"获取课程详情失败: {e}",
                            data={"match_info": match_info},
                        )

                # 课程文件列表
                files_data: List[dict] = []
                if include_files:
                    try:
                        files = await client.get_course_files(
                            resolved_id,
                            search_term=file_search_term,
                            content_types=file_content_types or None,
                        )
                        files_data = [f.to_dict() for f in files]
                        overview["files"] = files_data
                    except Exception as e:
                        return ToolResult(
                            success=False,
                            error="COURSE_FILES_FETCH_FAILED",
                            message=f"获取课程文件列表失败: {e}",
                            data={"match_info": match_info},
                        )

        except Exception as e:
            return ToolResult(
                success=False,
                error="CANVAS_FETCH_FAILED",
                message=f"Canvas 课程内容抓取失败: {e}",
            )

        msg_parts = ["Canvas 课程内容抓取完成"]
        if overview.get("course"):
            msg_parts.append("包含课程大纲")
        if overview.get("files"):
            msg_parts.append(f"文件 {len(overview.get('files', []))} 个")
        message = "，".join(msg_parts) + "。"

        return ToolResult(
            success=True,
            message=message,
            data={
                "course_content": overview,
                "match_info": match_info,
            },
            metadata={
                "source": "canvas",
                "type": "course_content",
            },
        )
