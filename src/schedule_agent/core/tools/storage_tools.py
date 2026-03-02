"""
存储工具 - 管理日程和任务的 CRUD 操作

提供 add_event, add_task, get_events, get_tasks 等工具。
"""

from datetime import datetime, date, timedelta
from typing import Optional, List, Any

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from schedule_agent.storage.json_repository import EventRepository, TaskRepository
from schedule_agent.models import Event, Task, EventStatus, TaskStatus, EventPriority, TaskPriority


def _datetime_sort_key(dt: datetime) -> float:
    """统一 datetime 排序键，兼容 offset-naive 与 offset-aware。"""
    return dt.timestamp()


class AddEventTool(BaseTool):
    """
    添加日程事件工具

    创建新的日程事件并保存到存储中。
    """

    def __init__(self, repository: Optional[EventRepository] = None):
        """
        初始化添加事件工具。

        Args:
            repository: 事件存储仓库（可选，默认创建新实例）
        """
        self._repository = repository or EventRepository()

    @property
    def name(self) -> str:
        return "add_event"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="add_event",
            description="""创建新的日程事件。

这是添加日程的主要工具，当用户想要:
- 添加新日程/会议/约会
- 安排某个活动
- 设置时间提醒

工具会自动:
- 检测时间冲突并提示
- 设置合理的默认值
- 为事件生成唯一 ID

事件(Event)与任务(Task)的区别:
- 事件有固定的开始和结束时间（如：明天下午3点到4点开会）
- 任务只有预计时长和截止日期，没有固定时间（如：写报告，预计2小时，周五前完成）""",
            parameters=[
                ToolParameter(
                    name="title",
                    type="string",
                    description="事件标题，简洁明了地描述这个事件",
                    required=True,
                ),
                ToolParameter(
                    name="start_time",
                    type="string",
                    description="开始时间，ISO 格式 (如: 2026-02-18T15:00:00)",
                    required=True,
                ),
                ToolParameter(
                    name="end_time",
                    type="string",
                    description="结束时间，ISO 格式 (如: 2026-02-18T16:00:00)",
                    required=True,
                ),
                ToolParameter(
                    name="description",
                    type="string",
                    description="事件详细描述（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="location",
                    type="string",
                    description="事件地点（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="priority",
                    type="string",
                    description="优先级：low（低）、medium（中）、high（高）、urgent（紧急）",
                    required=False,
                    enum=["low", "medium", "high", "urgent"],
                    default="medium",
                ),
                ToolParameter(
                    name="tags",
                    type="array",
                    description="标签列表，用于分类（可选，如: [\"工作\", \"会议\"]）",
                    required=False,
                ),
                ToolParameter(
                    name="source",
                    type="string",
                    description="事件来源：user/canvas/planner/course_import/system（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="event_type",
                    type="string",
                    description="事件类型：normal/course/deadline/planned_block（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="is_blocking",
                    type="boolean",
                    description="是否占用时间（可选，默认 true）",
                    required=False,
                ),
                ToolParameter(
                    name="origin_ref",
                    type="string",
                    description="外部来源引用 ID（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="linked_task_id",
                    type="string",
                    description="关联任务 ID（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="plan_run_id",
                    type="string",
                    description="规划批次 ID（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="metadata",
                    type="object",
                    description="附加结构化元数据（可选）",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "创建明天下午的团队会议",
                    "params": {
                        "title": "团队周会",
                        "start_time": "2026-02-18T15:00:00",
                        "end_time": "2026-02-18T16:00:00",
                        "description": "讨论本周工作进展",
                        "location": "会议室A",
                        "priority": "high",
                        "tags": ["工作", "会议"],
                    },
                },
                {
                    "description": "创建简单的约会",
                    "params": {
                        "title": "和朋友吃饭",
                        "start_time": "2026-02-19T18:00:00",
                        "end_time": "2026-02-19T20:00:00",
                    },
                },
            ],
            usage_notes=[
                "开始时间和结束时间必须是 ISO 格式",
                "结束时间必须晚于开始时间",
                "如果有时间冲突，工具会返回警告但仍会创建事件",
            ],
            tags=['日程', '创建'],
        )

    async def execute(self, **kwargs) -> ToolResult:
        """
        执行添加事件操作。

        Args:
            title: 事件标题
            start_time: 开始时间 (ISO 格式)
            end_time: 结束时间 (ISO 格式)
            description: 事件描述（可选）
            location: 地点（可选）
            priority: 优先级（可选，默认 medium）
            tags: 标签列表（可选）

        Returns:
            操作结果，包含创建的事件信息
        """
        # 验证必需参数
        title = kwargs.get("title")
        if not title:
            return ToolResult(
                success=False,
                error="MISSING_TITLE",
                message="缺少事件标题",
            )

        start_time_str = kwargs.get("start_time")
        end_time_str = kwargs.get("end_time")
        if not start_time_str or not end_time_str:
            return ToolResult(
                success=False,
                error="MISSING_TIME",
                message="缺少开始时间或结束时间",
            )

        try:
            # 解析时间
            start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            end_time = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
        except ValueError as e:
            return ToolResult(
                success=False,
                error="INVALID_TIME_FORMAT",
                message=f"时间格式无效: {str(e)}",
            )

        # 验证时间顺序
        if end_time <= start_time:
            return ToolResult(
                success=False,
                error="INVALID_TIME_RANGE",
                message="结束时间必须晚于开始时间",
            )

        # 解析优先级
        priority_str = kwargs.get("priority", "medium")
        try:
            priority = EventPriority(priority_str)
        except ValueError:
            priority = EventPriority.MEDIUM

        # 创建事件
        event = Event(
            title=title,
            description=kwargs.get("description"),
            start_time=start_time,
            end_time=end_time,
            location=kwargs.get("location"),
            priority=priority,
            tags=kwargs.get("tags", []),
            source=kwargs.get("source", "user"),
            event_type=kwargs.get("event_type", "normal"),
            is_blocking=kwargs.get("is_blocking", True),
            origin_ref=kwargs.get("origin_ref"),
            linked_task_id=kwargs.get("linked_task_id"),
            plan_run_id=kwargs.get("plan_run_id"),
            metadata=kwargs.get("metadata", {}) or {},
        )

        # 检查时间冲突
        conflicts = self._repository.find_conflicts(event)

        # 保存事件
        created_event = self._repository.create(event)

        # 构建结果消息
        message = f"成功创建事件: {created_event.title} ({created_event.id})"
        if conflicts:
            conflict_titles = [c.title for c in conflicts]
            message += f"\n警告: 与以下事件存在时间冲突: {', '.join(conflict_titles)}"

        return ToolResult(
            success=True,
            data=created_event,
            message=message,
            metadata={
                "event_id": created_event.id,
                "has_conflicts": len(conflicts) > 0,
                "conflict_count": len(conflicts),
            },
        )


class AddTaskTool(BaseTool):
    """
    添加任务工具

    创建新的任务并保存到存储中。
    """

    def __init__(self, repository: Optional[TaskRepository] = None):
        """
        初始化添加任务工具。

        Args:
            repository: 任务存储仓库（可选，默认创建新实例）
        """
        self._repository = repository or TaskRepository()

    @property
    def name(self) -> str:
        return "add_task"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="add_task",
            description="""创建新的待办任务。

这是添加任务的主要工具，当用户想要:
- 添加待办事项
- 创建需要完成的任务
- 设置任务的预计时长和截止日期

任务(Task)与事件(Event)的区别:
- 任务只有预计时长和截止日期，没有固定时间（如：写报告，预计2小时，周五前完成）
- 事件有固定的开始和结束时间（如：明天下午3点到4点开会）

创建的任务可以稍后由规划器自动安排到合适的时间段。""",
            parameters=[
                ToolParameter(
                    name="title",
                    type="string",
                    description="任务标题，简洁明了地描述这个任务",
                    required=True,
                ),
                ToolParameter(
                    name="estimated_minutes",
                    type="integer",
                    description="预计完成所需时间（分钟），默认60分钟",
                    required=False,
                    default=60,
                ),
                ToolParameter(
                    name="due_date",
                    type="string",
                    description="截止日期，格式: YYYY-MM-DD（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="description",
                    type="string",
                    description="任务详细描述（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="priority",
                    type="string",
                    description="优先级：low（低）、medium（中）、high（高）、urgent（紧急）",
                    required=False,
                    enum=["low", "medium", "high", "urgent"],
                    default="medium",
                ),
                ToolParameter(
                    name="tags",
                    type="array",
                    description="标签列表，用于分类（可选，如: [\"工作\", \"学习\"]）",
                    required=False,
                ),
                ToolParameter(
                    name="difficulty",
                    type="integer",
                    description="任务难度（1-5，可选，默认3）",
                    required=False,
                ),
                ToolParameter(
                    name="importance",
                    type="integer",
                    description="用户重视程度（1-5，可选，默认3）",
                    required=False,
                ),
                ToolParameter(
                    name="source",
                    type="string",
                    description="任务来源：user/canvas/planner/system（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="origin_ref",
                    type="string",
                    description="外部来源引用 ID（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="deadline_event_id",
                    type="string",
                    description="关联截止事件 ID（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="metadata",
                    type="object",
                    description="附加结构化元数据（可选）",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "创建一个工作任务",
                    "params": {
                        "title": "完成项目报告",
                        "estimated_minutes": 120,
                        "due_date": "2026-02-20",
                        "description": "整理本月项目进展并撰写报告",
                        "priority": "high",
                        "tags": ["工作", "报告"],
                    },
                },
                {
                    "description": "创建一个简单的待办事项",
                    "params": {
                        "title": "买牛奶",
                        "estimated_minutes": 30,
                    },
                },
            ],
            usage_notes=[
                "截止日期格式为 YYYY-MM-DD",
                "预计时长单位为分钟",
                "如果没有指定截止日期，任务不会过期",
                "任务创建后状态为待办(todo)，可以稍后安排时间",
            ],
            tags=['任务', '创建'],
        )

    async def execute(self, **kwargs) -> ToolResult:
        """
        执行添加任务操作。

        Args:
            title: 任务标题
            estimated_minutes: 预计时长（分钟）
            due_date: 截止日期（可选）
            description: 任务描述（可选）
            priority: 优先级（可选，默认 medium）
            tags: 标签列表（可选）

        Returns:
            操作结果，包含创建的任务信息
        """
        # 验证必需参数
        title = kwargs.get("title")
        if not title:
            return ToolResult(
                success=False,
                error="MISSING_TITLE",
                message="缺少任务标题",
            )

        # 解析截止日期
        due_date = None
        due_date_str = kwargs.get("due_date")
        if due_date_str:
            try:
                due_date = date.fromisoformat(due_date_str)
            except ValueError as e:
                return ToolResult(
                    success=False,
                    error="INVALID_DATE_FORMAT",
                    message=f"日期格式无效: {str(e)}，请使用 YYYY-MM-DD 格式",
                )

        # 解析优先级
        priority_str = kwargs.get("priority", "medium")
        try:
            priority = TaskPriority(priority_str)
        except ValueError:
            priority = TaskPriority.MEDIUM

        # 解析预计时长
        estimated_minutes = kwargs.get("estimated_minutes", 60)
        if not isinstance(estimated_minutes, int) or estimated_minutes < 1:
            estimated_minutes = 60

        difficulty = kwargs.get("difficulty", 3)
        if not isinstance(difficulty, int) or difficulty < 1 or difficulty > 5:
            difficulty = 3

        importance = kwargs.get("importance", 3)
        if not isinstance(importance, int) or importance < 1 or importance > 5:
            importance = 3

        # 创建任务
        task = Task(
            title=title,
            description=kwargs.get("description"),
            estimated_minutes=estimated_minutes,
            due_date=due_date,
            priority=priority,
            tags=kwargs.get("tags", []),
            difficulty=difficulty,
            importance=importance,
            source=kwargs.get("source", "user"),
            origin_ref=kwargs.get("origin_ref"),
            deadline_event_id=kwargs.get("deadline_event_id"),
            metadata=kwargs.get("metadata", {}) or {},
        )

        # 保存任务
        created_task = self._repository.create(task)

        # 构建结果消息
        message = f"成功创建任务: {created_task.title} ({created_task.id})"
        if due_date:
            message += f"，截止日期: {due_date}"

        return ToolResult(
            success=True,
            data=created_task,
            message=message,
            metadata={
                "task_id": created_task.id,
                "status": created_task.status.value,
                "is_overdue": created_task.is_overdue,
            },
        )


class GetEventsTool(BaseTool):
    """
    获取日程事件工具

    查询和筛选日程事件。
    """

    def __init__(self, repository: Optional[EventRepository] = None):
        """
        初始化获取事件工具。

        Args:
            repository: 事件存储仓库（可选，默认创建新实例）
        """
        self._repository = repository or EventRepository()

    @property
    def name(self) -> str:
        return "get_events"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_events",
            description="""查询日程事件。

这是查看日程的主要工具，支持多种查询方式:
- 查看今天/明天/本周的日程
- 按时间范围查询
- 按关键词搜索
- 查看所有日程

返回结果按开始时间排序。""",
            parameters=[
                ToolParameter(
                    name="query_type",
                    type="string",
                    description="查询类型：today（今天）、date（指定日期）、upcoming（即将到来）、range（时间范围）、all（所有）、search（搜索）",
                    required=False,
                    enum=["today", "date", "upcoming", "range", "all", "search"],
                    default="today",
                ),
                ToolParameter(
                    name="date",
                    type="string",
                    description="指定日期，格式: YYYY-MM-DD（推荐，查询某一天时优先使用）",
                    required=False,
                ),
                ToolParameter(
                    name="search_query",
                    type="string",
                    description="搜索关键词（仅在 query_type 为 search 时使用）",
                    required=False,
                ),
                ToolParameter(
                    name="days",
                    type="integer",
                    description="查询未来多少天的事件（仅在 query_type 为 upcoming 时使用，默认7天）",
                    required=False,
                    default=7,
                ),
                ToolParameter(
                    name="start_date",
                    type="string",
                    description="开始日期，格式: YYYY-MM-DD（可选，用于时间范围查询）",
                    required=False,
                ),
                ToolParameter(
                    name="end_date",
                    type="string",
                    description="结束日期，格式: YYYY-MM-DD（可选，用于时间范围查询）",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "查看今天的日程",
                    "params": {"query_type": "today"},
                },
                {
                    "description": "查看指定日期的日程（推荐）",
                    "params": {"date": "2026-02-27"},
                },
                {
                    "description": "查看未来7天的日程",
                    "params": {"query_type": "upcoming", "days": 7},
                },
                {
                    "description": "搜索包含'会议'的日程",
                    "params": {"query_type": "search", "search_query": "会议"},
                },
                {
                    "description": "查看所有日程",
                    "params": {"query_type": "all"},
                },
            ],
            usage_notes=[
                "默认查询今天的事件",
                "查询某一天时优先使用 date 参数（YYYY-MM-DD），比传 today 更准确",
                "搜索会匹配标题、描述和标签",
                "结果按开始时间升序排列",
            ],
            tags=['日程', '查询'],
        )

    async def execute(self, **kwargs) -> ToolResult:
        """
        执行查询事件操作。

        Args:
            query_type: 查询类型
            search_query: 搜索关键词
            days: 查询天数
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            操作结果，包含匹配的事件列表
        """
        query_type = kwargs.get("query_type", "today")
        query_date_str = kwargs.get("date")

        events: List[Event] = []
        message = ""

        # 优先支持 date 参数：即使 query_type 未显式传入，也能准确查询指定日期
        if query_date_str:
            try:
                target_date = date.fromisoformat(query_date_str)
            except ValueError:
                return ToolResult(
                    success=False,
                    error="INVALID_DATE_FORMAT",
                    message=f"日期格式无效: {query_date_str}，请使用 YYYY-MM-DD 格式",
                )
            events = self._repository.get_by_date(target_date)
            message = f"{query_date_str} 有 {len(events)} 个日程"
            query_type = "date"

        elif query_type == "today":
            events = self._repository.get_today()
            message = f"今天有 {len(events)} 个日程"

        elif query_type == "date":
            return ToolResult(
                success=False,
                error="MISSING_DATE",
                message="query_type=date 时需要提供 date 参数（YYYY-MM-DD）",
            )

        elif query_type == "upcoming":
            days = kwargs.get("days", 7)
            if not isinstance(days, int) or days < 1:
                days = 7
            events = self._repository.get_upcoming(days=days)
            message = f"未来 {days} 天有 {len(events)} 个日程"

        elif query_type == "search":
            search_query = kwargs.get("search_query", "")
            if not search_query:
                return ToolResult(
                    success=False,
                    error="MISSING_SEARCH_QUERY",
                    message="搜索关键词不能为空",
                )
            events = self._repository.search(search_query)
            message = f"找到 {len(events)} 个匹配的日程"

        elif query_type == "all":
            events = self._repository.get_all()
            # 过滤掉已取消的事件
            events = [e for e in events if e.status != EventStatus.CANCELLED]
            message = f"共有 {len(events)} 个日程"

        elif query_type == "range":
            # 按日期范围查询
            start_date_str = kwargs.get("start_date")
            end_date_str = kwargs.get("end_date")
            if not start_date_str or not end_date_str:
                return ToolResult(
                    success=False,
                    error="MISSING_DATE_RANGE",
                    message="时间范围查询需要提供 start_date 和 end_date",
                )
            try:
                start_dt = datetime.fromisoformat(start_date_str)
                end_dt = datetime.fromisoformat(end_date_str) + timedelta(days=1) - timedelta(seconds=1)
                events = self._repository.get_by_date_range(start_dt, end_dt)
                message = f"该时间段内有 {len(events)} 个日程"
            except ValueError as e:
                return ToolResult(
                    success=False,
                    error="INVALID_DATE_FORMAT",
                    message=f"日期格式无效: {str(e)}",
                )
        else:
            # 默认查询今天
            events = self._repository.get_today()
            message = f"今天有 {len(events)} 个日程"

        # 按开始时间排序（兼容 naive/aware 混合）
        events.sort(key=lambda e: _datetime_sort_key(e.start_time))

        return ToolResult(
            success=True,
            data=events,
            message=message,
            metadata={
                "query_type": query_type,
                "count": len(events),
            },
        )


class GetTasksTool(BaseTool):
    """
    获取任务工具

    查询和筛选任务。
    """

    def __init__(self, repository: Optional[TaskRepository] = None):
        """
        初始化获取任务工具。

        Args:
            repository: 任务存储仓库（可选，默认创建新实例）
        """
        self._repository = repository or TaskRepository()

    @property
    def name(self) -> str:
        return "get_tasks"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_tasks",
            description="""查询待办任务。

这是查看任务的主要工具，支持多种查询方式:
- 查看待办任务
- 查看已完成任务
- 查看过期任务
- 按关键词搜索
- 查看所有任务

返回结果按优先级和截止日期排序。""",
            parameters=[
                ToolParameter(
                    name="query_type",
                    type="string",
                    description="查询类型：todo（待办）、completed（已完成）、overdue（过期）、all（所有）、search（搜索）",
                    required=False,
                    enum=["todo", "completed", "overdue", "all", "search"],
                    default="todo",
                ),
                ToolParameter(
                    name="search_query",
                    type="string",
                    description="搜索关键词（仅在 query_type 为 search 时使用）",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "查看待办任务",
                    "params": {"query_type": "todo"},
                },
                {
                    "description": "查看已完成的任务",
                    "params": {"query_type": "completed"},
                },
                {
                    "description": "查看过期任务",
                    "params": {"query_type": "overdue"},
                },
                {
                    "description": "搜索包含'报告'的任务",
                    "params": {"query_type": "search", "search_query": "报告"},
                },
            ],
            usage_notes=[
                "默认查询待办任务",
                "搜索会匹配标题、描述和标签",
                "结果按优先级和截止日期排序",
            ],
            tags=['任务', '查询'],
        )

    async def execute(self, **kwargs) -> ToolResult:
        """
        执行查询任务操作。

        Args:
            query_type: 查询类型
            search_query: 搜索关键词

        Returns:
            操作结果，包含匹配的任务列表
        """
        query_type = kwargs.get("query_type", "todo")

        tasks: List[Task] = []
        message = ""

        if query_type == "todo":
            tasks = self._repository.get_todo()
            message = f"有 {len(tasks)} 个待办任务"

        elif query_type == "completed":
            tasks = self._repository.get_completed()
            message = f"已完成 {len(tasks)} 个任务"

        elif query_type == "overdue":
            tasks = self._repository.get_overdue()
            message = f"有 {len(tasks)} 个过期任务"

        elif query_type == "search":
            search_query = kwargs.get("search_query", "")
            if not search_query:
                return ToolResult(
                    success=False,
                    error="MISSING_SEARCH_QUERY",
                    message="搜索关键词不能为空",
                )
            tasks = self._repository.search(search_query)
            message = f"找到 {len(tasks)} 个匹配的任务"

        elif query_type == "all":
            tasks = self._repository.get_all()
            # 过滤掉已取消的任务
            tasks = [t for t in tasks if t.status != TaskStatus.CANCELLED]
            message = f"共有 {len(tasks)} 个任务"

        else:
            # 默认查询待办
            tasks = self._repository.get_todo()
            message = f"有 {len(tasks)} 个待办任务"

        # 按优先级和截止日期排序
        priority_order = {
            TaskPriority.URGENT: 0,
            TaskPriority.HIGH: 1,
            TaskPriority.MEDIUM: 2,
            TaskPriority.LOW: 3,
        }
        tasks.sort(key=lambda t: (
            priority_order.get(t.priority, 2),
            t.due_date or date.max,
        ))

        # 检测过期任务（排除已查询过期任务的情况）
        overdue_tasks = []
        if query_type != "overdue":
            overdue_tasks = [t for t in tasks if t.is_overdue]
        
        metadata = {
            "query_type": query_type,
            "count": len(tasks),
        }
        
        # 如果有过期任务，在 metadata 中标记
        if overdue_tasks:
            metadata["has_overdue"] = True
            metadata["overdue_count"] = len(overdue_tasks)
            metadata["overdue_task_ids"] = [t.id for t in overdue_tasks]

        return ToolResult(
            success=True,
            data=tasks,
            message=message,
            metadata=metadata,
        )


class UpdateEventTool(BaseTool):
    """
    更新事件工具

    支持更新事件状态、标题、时间、地点等字段。
    """

    def __init__(
        self,
        repository: Optional[EventRepository] = None,
        task_repository: Optional[TaskRepository] = None,
    ):
        self._repository = repository or EventRepository()
        self._task_repository = task_repository or TaskRepository()

    @property
    def name(self) -> str:
        return "update_event"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="update_event",
            description="""更新事件内容（状态、标题、时间等）。

这是修改事件的主要工具，当用户想要：
- 标记事件为已完成（"做完了""完成了""搞定了"）
- 取消事件
- 将事件标记为进行中
- 将事件重新设为已安排
- 调整事件时间（改期、提前、延后）
- 修改事件标题、地点、描述、优先级、标签

重要区分：
- 修改事件信息（状态/时间/标题/地点等）→ 使用本工具 update_event
- 永久删除事件 → 使用 delete_schedule_data
这两者是完全不同的操作，不可混淆。""",
            parameters=[
                ToolParameter(
                    name="event_id",
                    type="string",
                    description="要更新的事件 ID",
                    required=True,
                ),
                ToolParameter(
                    name="status",
                    type="string",
                    description="目标状态：completed（已完成）、cancelled（已取消）、in_progress（进行中）、scheduled（已安排）。如果不更新状态，可以不提供此参数。",
                    required=False,
                    enum=["completed", "cancelled", "in_progress", "scheduled"],
                ),
                ToolParameter(
                    name="title",
                    type="string",
                    description="新的事件标题（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="start_time",
                    type="string",
                    description="新的开始时间，ISO 格式（如 2026-02-18T15:00:00，可选）",
                    required=False,
                ),
                ToolParameter(
                    name="end_time",
                    type="string",
                    description="新的结束时间，ISO 格式（如 2026-02-18T16:00:00，可选）",
                    required=False,
                ),
                ToolParameter(
                    name="description",
                    type="string",
                    description="新的事件描述（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="location",
                    type="string",
                    description="新的事件地点（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="priority",
                    type="string",
                    description="新的优先级：low（低）、medium（中）、high（高）、urgent（紧急）（可选）",
                    required=False,
                    enum=["low", "medium", "high", "urgent"],
                ),
                ToolParameter(
                    name="tags",
                    type="array",
                    description="新的标签列表（可选，如 [\"工作\", \"会议\"]）",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "将事件标记为已完成",
                    "params": {"event_id": "a1b2c3d4", "status": "completed"},
                },
                {
                    "description": "取消一个事件",
                    "params": {"event_id": "a1b2c3d4", "status": "cancelled"},
                },
                {
                    "description": "将事件改为进行中",
                    "params": {"event_id": "a1b2c3d4", "status": "in_progress"},
                },
                {
                    "description": "调整事件时间",
                    "params": {
                        "event_id": "a1b2c3d4",
                        "start_time": "2026-02-18T16:00:00",
                        "end_time": "2026-02-18T17:00:00",
                    },
                },
                {
                    "description": "同时修改标题和地点",
                    "params": {
                        "event_id": "a1b2c3d4",
                        "title": "项目复盘会",
                        "location": "会议室B",
                    },
                },
            ],
            usage_notes=[
                "标记完成、取消和改期等都请使用本工具，不要使用 delete_schedule_data",
                '用户说「做完了」「完成了」「搞定了」「标记为已完成」等都应调用本工具',
                "用户说「改到X点」「延期到明天」「调整会议时间」等都应调用本工具并更新 start_time/end_time",
                "需要先通过 get_events 获取事件 ID",
                "status 与其他字段至少提供一个，可以同时更新多个字段",
            ],
            tags=['日程', '修改'],
        )

    async def execute(self, **kwargs) -> ToolResult:
        event_id = kwargs.get("event_id")
        if not event_id:
            return ToolResult(
                success=False,
                error="MISSING_EVENT_ID",
                message="缺少事件 ID",
            )

        status_str = kwargs.get("status")
        title = kwargs.get("title")
        start_time_str = kwargs.get("start_time")
        end_time_str = kwargs.get("end_time")
        update_fields = {
            "status": status_str,
            "title": title,
            "start_time": start_time_str,
            "end_time": end_time_str,
            "description": kwargs.get("description"),
            "location": kwargs.get("location"),
            "priority": kwargs.get("priority"),
            "tags": kwargs.get("tags"),
        }
        if all(value is None for value in update_fields.values()):
            return ToolResult(
                success=False,
                error="MISSING_UPDATE_PARAM",
                message="至少需要提供一个更新字段（如 status、title、start_time 等）",
            )

        event = self._repository.get(event_id)
        if not event:
            return ToolResult(
                success=False,
                error="EVENT_NOT_FOUND",
                message=f"未找到 ID 为 {event_id} 的事件",
            )

        old_status = event.status
        old_start_time = event.start_time
        old_end_time = event.end_time
        updates: List[str] = []
        linked_task_updated = False

        if status_str:
            try:
                target_status = EventStatus(status_str)
            except ValueError:
                return ToolResult(
                    success=False,
                    error="INVALID_STATUS",
                    message=f"无效的状态值: {status_str}，可选: scheduled, in_progress, completed, cancelled",
                )
            event.status = target_status
            updates.append(f"状态: {old_status.value} → {target_status.value}")

            # deadline 事件状态联动任务状态，保证双向一致
            if event.event_type == "deadline" and event.linked_task_id:
                linked_task = self._task_repository.get(event.linked_task_id)
                if linked_task:
                    if target_status == EventStatus.COMPLETED:
                        linked_task.mark_completed()
                        self._task_repository.update(linked_task)
                        linked_task_updated = True
                    elif target_status == EventStatus.CANCELLED:
                        linked_task.mark_cancelled()
                        self._task_repository.update(linked_task)
                        linked_task_updated = True

        if title is not None:
            if not isinstance(title, str) or not title.strip():
                return ToolResult(
                    success=False,
                    error="INVALID_TITLE",
                    message="title 必须是非空字符串",
                )
            old_title = event.title
            event.title = title
            updates.append(f"标题: {old_title} → {title}")

        if kwargs.get("description") is not None:
            old_description = event.description or "无"
            event.description = kwargs.get("description")
            new_description = event.description or "无"
            updates.append(f"描述: {old_description} → {new_description}")

        if kwargs.get("location") is not None:
            old_location = event.location or "无"
            event.location = kwargs.get("location")
            new_location = event.location or "无"
            updates.append(f"地点: {old_location} → {new_location}")

        if kwargs.get("priority") is not None:
            priority_str = kwargs.get("priority")
            try:
                new_priority = EventPriority(priority_str)
            except ValueError:
                return ToolResult(
                    success=False,
                    error="INVALID_PRIORITY",
                    message=f"无效的优先级: {priority_str}，可选: low, medium, high, urgent",
                )
            old_priority = event.priority
            event.priority = new_priority
            updates.append(f"优先级: {old_priority.value} → {new_priority.value}")

        if kwargs.get("tags") is not None:
            tags = kwargs.get("tags")
            if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                return ToolResult(
                    success=False,
                    error="INVALID_TAGS",
                    message="tags 必须是字符串数组",
                )
            old_tags = event.tags
            event.tags = tags
            updates.append(f"标签: {old_tags} → {tags}")

        if start_time_str is not None:
            try:
                new_start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
            except ValueError as e:
                return ToolResult(
                    success=False,
                    error="INVALID_TIME_FORMAT",
                    message=f"开始时间格式无效: {str(e)}",
                )
            event.start_time = new_start_time
            updates.append(
                f"开始时间: {old_start_time.isoformat()} → {new_start_time.isoformat()}"
            )

        if end_time_str is not None:
            try:
                new_end_time = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
            except ValueError as e:
                return ToolResult(
                    success=False,
                    error="INVALID_TIME_FORMAT",
                    message=f"结束时间格式无效: {str(e)}",
                )
            event.end_time = new_end_time
            updates.append(
                f"结束时间: {old_end_time.isoformat()} → {new_end_time.isoformat()}"
            )

        if event.end_time <= event.start_time:
            return ToolResult(
                success=False,
                error="INVALID_TIME_RANGE",
                message="结束时间必须晚于开始时间",
            )

        event.update_timestamp()

        self._repository.update(event)

        conflicts = self._repository.find_conflicts(event, exclude_id=event.id)

        status_labels = {
            EventStatus.SCHEDULED: "已安排",
            EventStatus.IN_PROGRESS: "进行中",
            EventStatus.COMPLETED: "已完成",
            EventStatus.CANCELLED: "已取消",
        }

        message = f"事件「{event.title}」已更新：{', '.join(updates)}"
        if conflicts:
            conflict_titles = [c.title for c in conflicts]
            message += f"\n警告: 更新时间后与以下事件存在冲突: {', '.join(conflict_titles)}"

        return ToolResult(
            success=True,
            data=event,
            message=message,
            metadata={
                "event_id": event.id,
                "old_status": old_status.value,
                "new_status": event.status.value,
                "updated_fields": [k for k, v in update_fields.items() if v is not None],
                "has_conflicts": len(conflicts) > 0,
                "conflict_count": len(conflicts),
                "status_label_before": status_labels[old_status],
                "status_label_after": status_labels[event.status],
                "linked_task_updated": linked_task_updated,
            },
        )


class UpdateTaskTool(BaseTool):
    """
    更新任务状态工具

    支持将任务标记为已完成、已取消、进行中或待办。
    """

    def __init__(
        self,
        repository: Optional[TaskRepository] = None,
        event_repository: Optional[EventRepository] = None,
    ):
        self._repository = repository or TaskRepository()
        self._event_repository = event_repository or EventRepository()

    @property
    def name(self) -> str:
        return "update_task"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="update_task",
            description="""更新任务的状态或截止日期。

这是修改任务的主要工具，当用户想要：
- 标记任务为已完成（"做完了""完成了""搞定了"）
- 取消任务
- 将任务标记为进行中
- 将任务重新设为待办
- 调整任务的截止日期（延期、修改截止时间等）

重要区分：
- 修改任务状态（完成/取消/进行中/待办）→ 使用本工具 update_task
- 修改任务截止日期 → 使用本工具 update_task（提供 due_date 参数）
- 永久删除任务 → 使用 delete_schedule_data
这两者是完全不同的操作，不可混淆。""",
            parameters=[
                ToolParameter(
                    name="task_id",
                    type="string",
                    description="要更新的任务 ID",
                    required=True,
                ),
                ToolParameter(
                    name="status",
                    type="string",
                    description="目标状态：completed（已完成）、cancelled（已取消）、in_progress（进行中）、todo（待办）。如果不更新状态，可以不提供此参数。",
                    required=False,
                    enum=["completed", "cancelled", "in_progress", "todo"],
                ),
                ToolParameter(
                    name="due_date",
                    type="string",
                    description="新的截止日期，格式：YYYY-MM-DD（如 2026-02-25）。如果不更新截止日期，可以不提供此参数。",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "将任务标记为已完成",
                    "params": {"task_id": "a1b2c3d4", "status": "completed"},
                },
                {
                    "description": "取消一个任务",
                    "params": {"task_id": "a1b2c3d4", "status": "cancelled"},
                },
                {
                    "description": "将任务改为进行中",
                    "params": {"task_id": "a1b2c3d4", "status": "in_progress"},
                },
                {
                    "description": "更新任务的截止日期",
                    "params": {"task_id": "a1b2c3d4", "due_date": "2026-02-25"},
                },
                {
                    "description": "同时更新状态和截止日期",
                    "params": {"task_id": "a1b2c3d4", "status": "todo", "due_date": "2026-02-25"},
                },
            ],
            usage_notes=[
                "标记完成、取消等状态变更请使用本工具，不要使用 delete_schedule_data",
                '用户说「做完了」「完成了」「搞定了」「标记为已完成」等都应调用本工具',
                "用户说「延期」「调整截止日期」「改到X号」等应使用本工具更新 due_date",
                "需要先通过 get_tasks 获取任务 ID",
                "status 和 due_date 至少提供一个，可以同时提供",
            ],
            tags=['任务', '修改'],
        )

    async def execute(self, **kwargs) -> ToolResult:
        task_id = kwargs.get("task_id")
        if not task_id:
            return ToolResult(
                success=False,
                error="MISSING_TASK_ID",
                message="缺少任务 ID",
            )

        status_str = kwargs.get("status")
        due_date_str = kwargs.get("due_date")

        # 至少需要提供一个更新参数
        if not status_str and not due_date_str:
            return ToolResult(
                success=False,
                error="MISSING_UPDATE_PARAM",
                message="至少需要提供 status 或 due_date 参数之一",
            )

        task = self._repository.get(task_id)
        if not task:
            return ToolResult(
                success=False,
                error="TASK_NOT_FOUND",
                message=f"未找到 ID 为 {task_id} 的任务",
            )

        old_status = task.status
        old_due_date = task.due_date
        updates = []
        linked_event_updated = False

        # 更新状态
        if status_str:
            try:
                target_status = TaskStatus(status_str)
            except ValueError:
                return ToolResult(
                    success=False,
                    error="INVALID_STATUS",
                    message=f"无效的状态值: {status_str}，可选: todo, in_progress, completed, cancelled",
                )

            if target_status == TaskStatus.COMPLETED:
                task.mark_completed()
            elif target_status == TaskStatus.CANCELLED:
                task.mark_cancelled()
            else:
                task.status = target_status
                task.update_timestamp()
            
            updates.append(f"状态: {old_status.value} → {target_status.value}")

            # 任务状态联动截止事件：确保作业完成后任务与时间同步完成
            if task.deadline_event_id:
                linked_event = self._event_repository.get(task.deadline_event_id)
                if linked_event:
                    if target_status == TaskStatus.COMPLETED:
                        linked_event.status = EventStatus.COMPLETED
                        linked_event.update_timestamp()
                        self._event_repository.update(linked_event)
                        linked_event_updated = True
                    elif target_status == TaskStatus.CANCELLED:
                        linked_event.status = EventStatus.CANCELLED
                        linked_event.update_timestamp()
                        self._event_repository.update(linked_event)
                        linked_event_updated = True

        # 更新截止日期
        if due_date_str:
            try:
                new_due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
                task.due_date = new_due_date
                task.update_timestamp()
                old_date_str = old_due_date.strftime("%Y-%m-%d") if old_due_date else "无"
                updates.append(f"截止日期: {old_date_str} → {due_date_str}")
            except ValueError:
                return ToolResult(
                    success=False,
                    error="INVALID_DATE_FORMAT",
                    message=f"日期格式无效: {due_date_str}，请使用 YYYY-MM-DD 格式（如 2026-02-25）",
                )

        self._repository.update(task)

        status_labels = {
            TaskStatus.TODO: "待办",
            TaskStatus.IN_PROGRESS: "进行中",
            TaskStatus.COMPLETED: "已完成",
            TaskStatus.CANCELLED: "已取消",
        }

        message = f"任务「{task.title}」已更新：{', '.join(updates)}"

        metadata = {
            "task_id": task.id,
        }
        if status_str:
            metadata["old_status"] = old_status.value
            metadata["new_status"] = task.status.value
        if due_date_str:
            metadata["old_due_date"] = old_due_date.isoformat() if old_due_date else None
            metadata["new_due_date"] = task.due_date.isoformat() if task.due_date else None
        metadata["linked_event_updated"] = linked_event_updated

        return ToolResult(
            success=True,
            data=task,
            message=message,
            metadata=metadata,
        )


class DeleteScheduleDataTool(BaseTool):
    """
    删除日程/任务工具

    支持删除单条、多条或全量数据。通过用户确认（yes/no）做基本审核。
    """

    def __init__(
        self,
        event_repository: Optional[EventRepository] = None,
        task_repository: Optional[TaskRepository] = None,
    ):
        """
        初始化删除工具。

        Args:
            event_repository: 事件仓库（可选）
            task_repository: 任务仓库（可选）
        """
        self._event_repository = event_repository or EventRepository()
        self._task_repository = task_repository or TaskRepository()

    @property
    def name(self) -> str:
        return "delete_schedule_data"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="delete_schedule_data",
            description="""永久删除事件或任务（不可恢复）。

本工具仅用于「永久删除」，不可用于状态变更。

重要语义区分：
- 用户说"完成了""搞定了""标记为已完成" → 必须使用 update_task，绝对不可调用本工具
- 用户说"取消任务" → 必须使用 update_task（status=cancelled），绝对不可调用本工具
- 只有用户明确表达"删除""移除""清除"等永久删除意图时，才可使用本工具

支持以下删除模式：
- 单条删除：按 ID 删除单个事件/任务
- 批量删除：按 ID 列表删除多条记录
- 全量删除：删除该类型下全部记录（delete_all=true）

删除确认流程：
1. 用户提出删除请求时，先用 get_events/get_tasks 查出将要删除的条目，向用户列出并询问「确认删除吗？请回复 是/确认/yes 以执行」；
2. 仅当用户明确回复 是、确认、yes 等肯定意图后，再调用本工具并传 confirm=true 执行删除。
3. "标记完成""已完成""做完了"等表述绝不是删除确认。""",
            parameters=[
                ToolParameter(
                    name="resource_type",
                    type="string",
                    description="资源类型：event（事件）或 task（任务）",
                    required=True,
                    enum=["event", "task"],
                ),
                ToolParameter(
                    name="target_ids",
                    type="array",
                    description="要删除的 ID 列表（单条或批量删除使用）",
                    required=False,
                ),
                ToolParameter(
                    name="delete_all",
                    type="boolean",
                    description="是否删除该类型下全部记录（默认 false）",
                    required=False,
                    default=False,
                ),
                ToolParameter(
                    name="confirm",
                    type="boolean",
                    description="是否已获用户确认（用户回复 是/确认/yes 后才设为 true）",
                    required=True,
                ),
            ],
            examples=[
                {
                    "description": "删除单个任务（用户已确认）",
                    "params": {
                        "resource_type": "task",
                        "target_ids": ["a1b2c3d4"],
                        "confirm": True,
                    },
                },
                {
                    "description": "批量删除多个事件（用户已确认）",
                    "params": {
                        "resource_type": "event",
                        "target_ids": ["e1", "e2", "e3"],
                        "confirm": True,
                    },
                },
                {
                    "description": "全量删除任务（用户已确认）",
                    "params": {
                        "resource_type": "task",
                        "delete_all": True,
                        "confirm": True,
                    },
                },
            ],
            usage_notes=[
                "删除操作不可恢复。批量/全量删除前务必先列出待删项并让用户确认（是/yes/确认）后再调用并传 confirm=true。",
                'confirm=true 仅在用户明确同意「删除」时才可设置。「标记完成」「已完成」「做完了」等表述是状态更新，不是删除确认，应使用 update_task。',
                "如果用户的原始请求不是删除，而是标记完成或取消，请改用 update_task 工具。",
            ],
            tags=['日程', '任务', '删除'],
        )

    def _get_repository(self, resource_type: str):
        if resource_type == "event":
            return self._event_repository
        return self._task_repository

    @staticmethod
    def _deduplicate_ids(raw_ids: List[str]) -> List[str]:
        seen = set()
        deduped: List[str] = []
        for item_id in raw_ids:
            if isinstance(item_id, str) and item_id and item_id not in seen:
                seen.add(item_id)
                deduped.append(item_id)
        return deduped

    async def execute(self, **kwargs) -> ToolResult:
        """
        执行删除操作。

        Args:
            resource_type: 资源类型（event/task）
            target_ids: 目标 ID 列表
            delete_all: 是否全量删除
            confirm: 是否已获用户确认（是/确认/yes）

        Returns:
            删除结果
        """
        resource_type = kwargs.get("resource_type")
        if resource_type not in ("event", "task"):
            return ToolResult(
                success=False,
                error="INVALID_RESOURCE_TYPE",
                message="resource_type 必须是 event 或 task",
            )

        confirm = kwargs.get("confirm", False)
        if confirm is not True:
            return ToolResult(
                success=False,
                error="CONFIRMATION_REQUIRED",
                message="删除操作需要用户确认。请先向用户列出待删项，待用户回复 是/确认/yes 后再调用并传 confirm=true",
            )

        delete_all = kwargs.get("delete_all", False)
        raw_target_ids = kwargs.get("target_ids", [])
        if raw_target_ids is None:
            raw_target_ids = []
        if not isinstance(raw_target_ids, list):
            return ToolResult(
                success=False,
                error="INVALID_TARGET_IDS",
                message="target_ids 必须是数组类型",
            )
        target_ids = self._deduplicate_ids(raw_target_ids)

        if delete_all and target_ids:
            return ToolResult(
                success=False,
                error="CONFLICTING_PARAMETERS",
                message="delete_all=true 时不应提供 target_ids",
            )

        repository = self._get_repository(resource_type)
        mode = "single"

        if delete_all:
            mode = "all"
            target_ids = [item.id for item in repository.get_all()]
        elif not target_ids:
            return ToolResult(
                success=False,
                error="MISSING_TARGET_IDS",
                message="请提供 target_ids，或设置 delete_all=true",
            )
        elif len(target_ids) > 1:
            mode = "batch"

        deleted_ids: List[str] = []
        not_found_ids: List[str] = []

        for item_id in target_ids:
            if repository.delete(item_id):
                deleted_ids.append(item_id)
            else:
                not_found_ids.append(item_id)

        if not deleted_ids:
            return ToolResult(
                success=False,
                error="NOTHING_DELETED",
                message=f"未删除任何 {resource_type}，目标可能不存在",
                metadata={
                    "resource_type": resource_type,
                    "mode": mode,
                    "not_found_ids": not_found_ids,
                },
            )

        message = f"成功删除 {len(deleted_ids)} 条{resource_type}记录"
        if not_found_ids:
            message += f"，另有 {len(not_found_ids)} 条未找到"

        return ToolResult(
            success=True,
            data={
                "deleted_ids": deleted_ids,
                "not_found_ids": not_found_ids,
            },
            message=message,
            metadata={
                "resource_type": resource_type,
                "mode": mode,
                "deleted_count": len(deleted_ids),
                "not_found_count": len(not_found_ids),
            },
        )
