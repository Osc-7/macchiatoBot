"""
存储工具测试 - 测试 add_event, add_task, get_events, get_tasks 工具
"""

import os
import tempfile
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from schedule_agent.core.tools.storage_tools import (
    AddEventTool,
    AddTaskTool,
    GetEventsTool,
    GetTasksTool,
    UpdateEventTool,
    UpdateTaskTool,
    DeleteScheduleDataTool,
)
from schedule_agent.core.tools.base import ToolDefinition
from schedule_agent.storage.json_repository import EventRepository, TaskRepository
from schedule_agent.models import Event, Task, EventStatus, TaskStatus, EventPriority, TaskPriority


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_dir():
    """创建临时目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def event_repository(temp_dir):
    """创建事件存储仓库"""
    file_path = temp_dir / "test_events.json"
    return EventRepository(file_path)


@pytest.fixture
def task_repository(temp_dir):
    """创建任务存储仓库"""
    file_path = temp_dir / "test_tasks.json"
    return TaskRepository(file_path)


@pytest.fixture
def add_event_tool(event_repository):
    """创建添加事件工具"""
    return AddEventTool(repository=event_repository)


@pytest.fixture
def add_task_tool(task_repository):
    """创建添加任务工具"""
    return AddTaskTool(repository=task_repository)


@pytest.fixture
def get_events_tool(event_repository):
    """创建获取事件工具"""
    return GetEventsTool(repository=event_repository)


@pytest.fixture
def get_tasks_tool(task_repository):
    """创建获取任务工具"""
    return GetTasksTool(repository=task_repository)


@pytest.fixture
def update_event_tool(event_repository):
    """创建更新事件工具"""
    return UpdateEventTool(repository=event_repository)


@pytest.fixture
def update_task_tool(task_repository):
    """创建更新任务工具"""
    return UpdateTaskTool(repository=task_repository)


@pytest.fixture
def delete_tool(event_repository, task_repository):
    """创建删除工具"""
    return DeleteScheduleDataTool(
        event_repository=event_repository,
        task_repository=task_repository,
    )


# ============================================================================
# AddEventTool 测试
# ============================================================================

class TestAddEventTool:
    """AddEventTool 测试类"""

    def test_name(self, add_event_tool):
        """测试工具名称"""
        assert add_event_tool.name == "add_event"

    def test_get_definition(self, add_event_tool):
        """测试获取工具定义"""
        definition = add_event_tool.get_definition()
        assert isinstance(definition, ToolDefinition)
        assert definition.name == "add_event"
        assert len(definition.parameters) == 7
        assert definition.parameters[0].name == "title"
        assert definition.parameters[0].required is True

    @pytest.mark.asyncio
    async def test_execute_success(self, add_event_tool):
        """测试成功创建事件"""
        tomorrow = date.today() + timedelta(days=1)
        result = await add_event_tool.execute(
            title="团队会议",
            start_time=f"{tomorrow}T15:00:00",
            end_time=f"{tomorrow}T16:00:00",
            description="讨论项目进展",
            location="会议室A",
            priority="high",
            tags=["工作", "会议"],
        )

        assert result.success is True
        assert result.data is not None
        assert result.data.title == "团队会议"
        assert result.data.location == "会议室A"
        assert result.data.priority == EventPriority.HIGH
        assert "工作" in result.data.tags
        assert "成功创建事件" in result.message

    @pytest.mark.asyncio
    async def test_execute_minimal(self, add_event_tool):
        """测试最简参数创建事件"""
        tomorrow = date.today() + timedelta(days=1)
        result = await add_event_tool.execute(
            title="简单事件",
            start_time=f"{tomorrow}T10:00:00",
            end_time=f"{tomorrow}T11:00:00",
        )

        assert result.success is True
        assert result.data.title == "简单事件"
        assert result.data.priority == EventPriority.MEDIUM
        assert result.data.tags == []

    @pytest.mark.asyncio
    async def test_execute_missing_title(self, add_event_tool):
        """测试缺少标题"""
        result = await add_event_tool.execute(
            start_time="2026-02-18T15:00:00",
            end_time="2026-02-18T16:00:00",
        )

        assert result.success is False
        assert result.error == "MISSING_TITLE"
        assert "缺少事件标题" in result.message

    @pytest.mark.asyncio
    async def test_execute_missing_time(self, add_event_tool):
        """测试缺少时间"""
        result = await add_event_tool.execute(title="测试事件")

        assert result.success is False
        assert result.error == "MISSING_TIME"

    @pytest.mark.asyncio
    async def test_execute_invalid_time_format(self, add_event_tool):
        """测试无效时间格式"""
        result = await add_event_tool.execute(
            title="测试事件",
            start_time="invalid-time",
            end_time="2026-02-18T16:00:00",
        )

        assert result.success is False
        assert result.error == "INVALID_TIME_FORMAT"

    @pytest.mark.asyncio
    async def test_execute_end_before_start(self, add_event_tool):
        """测试结束时间早于开始时间"""
        result = await add_event_tool.execute(
            title="测试事件",
            start_time="2026-02-18T16:00:00",
            end_time="2026-02-18T15:00:00",
        )

        assert result.success is False
        assert result.error == "INVALID_TIME_RANGE"
        assert "结束时间必须晚于开始时间" in result.message

    @pytest.mark.asyncio
    async def test_execute_with_conflict(self, add_event_tool, event_repository):
        """测试时间冲突检测"""
        tomorrow = date.today() + timedelta(days=1)

        # 创建第一个事件
        await add_event_tool.execute(
            title="第一个会议",
            start_time=f"{tomorrow}T10:00:00",
            end_time=f"{tomorrow}T12:00:00",
        )

        # 创建有冲突的第二个事件
        result = await add_event_tool.execute(
            title="第二个会议",
            start_time=f"{tomorrow}T11:00:00",
            end_time=f"{tomorrow}T13:00:00",
        )

        assert result.success is True
        assert result.metadata["has_conflicts"] is True
        assert "时间冲突" in result.message

    @pytest.mark.asyncio
    async def test_execute_with_timezone_z(self, add_event_tool):
        """测试带 Z 后缀的时间格式"""
        tomorrow = date.today() + timedelta(days=1)
        result = await add_event_tool.execute(
            title="测试事件",
            start_time=f"{tomorrow}T10:00:00Z",
            end_time=f"{tomorrow}T11:00:00Z",
        )

        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_invalid_priority(self, add_event_tool):
        """测试无效优先级（应使用默认值）"""
        tomorrow = date.today() + timedelta(days=1)
        result = await add_event_tool.execute(
            title="测试事件",
            start_time=f"{tomorrow}T10:00:00",
            end_time=f"{tomorrow}T11:00:00",
            priority="invalid_priority",
        )

        assert result.success is True
        assert result.data.priority == EventPriority.MEDIUM


# ============================================================================
# AddTaskTool 测试
# ============================================================================

class TestAddTaskTool:
    """AddTaskTool 测试类"""

    def test_name(self, add_task_tool):
        """测试工具名称"""
        assert add_task_tool.name == "add_task"

    def test_get_definition(self, add_task_tool):
        """测试获取工具定义"""
        definition = add_task_tool.get_definition()
        assert isinstance(definition, ToolDefinition)
        assert definition.name == "add_task"
        assert len(definition.parameters) == 6

    @pytest.mark.asyncio
    async def test_execute_success(self, add_task_tool):
        """测试成功创建任务"""
        result = await add_task_tool.execute(
            title="完成报告",
            estimated_minutes=120,
            due_date="2026-02-20",
            description="整理项目进展",
            priority="high",
            tags=["工作", "报告"],
        )

        assert result.success is True
        assert result.data is not None
        assert result.data.title == "完成报告"
        assert result.data.estimated_minutes == 120
        assert result.data.due_date == date(2026, 2, 20)
        assert result.data.priority == TaskPriority.HIGH
        assert "成功创建任务" in result.message

    @pytest.mark.asyncio
    async def test_execute_minimal(self, add_task_tool):
        """测试最简参数创建任务"""
        result = await add_task_tool.execute(title="简单任务")

        assert result.success is True
        assert result.data.title == "简单任务"
        assert result.data.estimated_minutes == 60  # 默认值
        assert result.data.due_date is None
        assert result.data.priority == TaskPriority.MEDIUM

    @pytest.mark.asyncio
    async def test_execute_missing_title(self, add_task_tool):
        """测试缺少标题"""
        result = await add_task_tool.execute(
            estimated_minutes=60,
            due_date="2026-02-20",
        )

        assert result.success is False
        assert result.error == "MISSING_TITLE"
        assert "缺少任务标题" in result.message

    @pytest.mark.asyncio
    async def test_execute_invalid_date_format(self, add_task_tool):
        """测试无效日期格式"""
        result = await add_task_tool.execute(
            title="测试任务",
            due_date="invalid-date",
        )

        assert result.success is False
        assert result.error == "INVALID_DATE_FORMAT"

    @pytest.mark.asyncio
    async def test_execute_invalid_priority(self, add_task_tool):
        """测试无效优先级（应使用默认值）"""
        result = await add_task_tool.execute(
            title="测试任务",
            priority="invalid",
        )

        assert result.success is True
        assert result.data.priority == TaskPriority.MEDIUM

    @pytest.mark.asyncio
    async def test_execute_invalid_estimated_minutes(self, add_task_tool):
        """测试无效预计时长（应使用默认值）"""
        result = await add_task_tool.execute(
            title="测试任务",
            estimated_minutes=-10,
        )

        assert result.success is True
        assert result.data.estimated_minutes == 60  # 使用默认值

    @pytest.mark.asyncio
    async def test_execute_with_string_estimated_minutes(self, add_task_tool):
        """测试字符串形式的预计时长"""
        result = await add_task_tool.execute(
            title="测试任务",
            estimated_minutes="60",  # 字符串形式
        )

        # 由于类型检查，字符串会被判断为非 int
        assert result.success is True
        assert result.data.estimated_minutes == 60  # 使用默认值


# ============================================================================
# GetEventsTool 测试
# ============================================================================

class TestGetEventsTool:
    """GetEventsTool 测试类"""

    def test_name(self, get_events_tool):
        """测试工具名称"""
        assert get_events_tool.name == "get_events"

    def test_get_definition(self, get_events_tool):
        """测试获取工具定义"""
        definition = get_events_tool.get_definition()
        assert isinstance(definition, ToolDefinition)
        assert definition.name == "get_events"

    @pytest.mark.asyncio
    async def test_execute_today_empty(self, get_events_tool):
        """测试查询今天（空）"""
        result = await get_events_tool.execute(query_type="today")

        assert result.success is True
        assert result.data == []
        assert "今天有 0 个日程" in result.message

    @pytest.mark.asyncio
    async def test_execute_today_with_events(self, get_events_tool, event_repository):
        """测试查询今天（有事件）"""
        today = date.today()
        now = datetime.now()

        # 创建今天的事件
        event = Event(
            title="今天的事件",
            start_time=datetime.combine(today, datetime.min.time().replace(hour=10)),
            end_time=datetime.combine(today, datetime.min.time().replace(hour=11)),
        )
        event_repository.create(event)

        result = await get_events_tool.execute(query_type="today")

        assert result.success is True
        assert len(result.data) == 1
        assert result.data[0].title == "今天的事件"

    @pytest.mark.asyncio
    async def test_execute_upcoming(self, get_events_tool, event_repository):
        """测试查询即将到来"""
        tomorrow = date.today() + timedelta(days=1)

        # 创建明天的事件
        event = Event(
            title="明天的事件",
            start_time=datetime.combine(tomorrow, datetime.min.time().replace(hour=10)),
            end_time=datetime.combine(tomorrow, datetime.min.time().replace(hour=11)),
        )
        event_repository.create(event)

        result = await get_events_tool.execute(query_type="upcoming", days=7)

        assert result.success is True
        assert len(result.data) == 1
        assert "未来 7 天" in result.message

    @pytest.mark.asyncio
    async def test_execute_search(self, get_events_tool, event_repository):
        """测试搜索事件"""
        today = date.today()

        # 创建多个事件
        event1 = Event(
            title="团队会议",
            start_time=datetime.combine(today, datetime.min.time().replace(hour=10)),
            end_time=datetime.combine(today, datetime.min.time().replace(hour=11)),
        )
        event2 = Event(
            title="项目评审",
            start_time=datetime.combine(today, datetime.min.time().replace(hour=14)),
            end_time=datetime.combine(today, datetime.min.time().replace(hour=15)),
        )
        event_repository.create(event1)
        event_repository.create(event2)

        result = await get_events_tool.execute(query_type="search", search_query="会议")

        assert result.success is True
        assert len(result.data) == 1
        assert result.data[0].title == "团队会议"

    @pytest.mark.asyncio
    async def test_execute_search_empty_query(self, get_events_tool):
        """测试搜索关键词为空"""
        result = await get_events_tool.execute(query_type="search", search_query="")

        assert result.success is False
        assert result.error == "MISSING_SEARCH_QUERY"

    @pytest.mark.asyncio
    async def test_execute_all(self, get_events_tool, event_repository):
        """测试查询所有事件"""
        today = date.today()

        # 创建多个事件
        for i in range(3):
            event = Event(
                title=f"事件{i}",
                start_time=datetime.combine(today, datetime.min.time().replace(hour=9+i)),
                end_time=datetime.combine(today, datetime.min.time().replace(hour=10+i)),
            )
            event_repository.create(event)

        result = await get_events_tool.execute(query_type="all")

        assert result.success is True
        assert len(result.data) == 3

    @pytest.mark.asyncio
    async def test_execute_all_excludes_cancelled(self, get_events_tool, event_repository):
        """测试查询所有事件排除已取消"""
        today = date.today()

        # 创建事件
        event1 = Event(
            title="正常事件",
            start_time=datetime.combine(today, datetime.min.time().replace(hour=10)),
            end_time=datetime.combine(today, datetime.min.time().replace(hour=11)),
        )
        event2 = Event(
            title="已取消事件",
            start_time=datetime.combine(today, datetime.min.time().replace(hour=14)),
            end_time=datetime.combine(today, datetime.min.time().replace(hour=15)),
            status=EventStatus.CANCELLED,
        )
        event_repository.create(event1)
        event_repository.create(event2)

        result = await get_events_tool.execute(query_type="all")

        assert result.success is True
        assert len(result.data) == 1
        assert result.data[0].title == "正常事件"

    @pytest.mark.asyncio
    async def test_execute_default_to_today(self, get_events_tool):
        """测试默认查询今天"""
        result = await get_events_tool.execute()

        assert result.success is True
        assert result.metadata["query_type"] == "today"

    @pytest.mark.asyncio
    async def test_execute_events_sorted_by_time(self, get_events_tool, event_repository):
        """测试事件按时间排序"""
        today = date.today()

        # 创建乱序的事件
        event1 = Event(
            title="下午事件",
            start_time=datetime.combine(today, datetime.min.time().replace(hour=14)),
            end_time=datetime.combine(today, datetime.min.time().replace(hour=15)),
        )
        event2 = Event(
            title="上午事件",
            start_time=datetime.combine(today, datetime.min.time().replace(hour=9)),
            end_time=datetime.combine(today, datetime.min.time().replace(hour=10)),
        )
        event_repository.create(event1)
        event_repository.create(event2)

        result = await get_events_tool.execute(query_type="all")

        assert result.success is True
        assert len(result.data) == 2
        # 应该按时间升序排列
        assert result.data[0].title == "上午事件"
        assert result.data[1].title == "下午事件"


# ============================================================================
# GetTasksTool 测试
# ============================================================================

class TestGetTasksTool:
    """GetTasksTool 测试类"""

    def test_name(self, get_tasks_tool):
        """测试工具名称"""
        assert get_tasks_tool.name == "get_tasks"

    def test_get_definition(self, get_tasks_tool):
        """测试获取工具定义"""
        definition = get_tasks_tool.get_definition()
        assert isinstance(definition, ToolDefinition)
        assert definition.name == "get_tasks"

    @pytest.mark.asyncio
    async def test_execute_todo_empty(self, get_tasks_tool):
        """测试查询待办（空）"""
        result = await get_tasks_tool.execute(query_type="todo")

        assert result.success is True
        assert result.data == []
        assert "有 0 个待办任务" in result.message

    @pytest.mark.asyncio
    async def test_execute_todo_with_tasks(self, get_tasks_tool, task_repository):
        """测试查询待办任务"""
        # 创建待办任务
        task = Task(title="待办任务", status=TaskStatus.TODO)
        task_repository.create(task)

        result = await get_tasks_tool.execute(query_type="todo")

        assert result.success is True
        assert len(result.data) == 1
        assert result.data[0].title == "待办任务"

    @pytest.mark.asyncio
    async def test_execute_completed(self, get_tasks_tool, task_repository):
        """测试查询已完成任务"""
        # 创建已完成的任务
        task = Task(title="已完成任务", status=TaskStatus.COMPLETED)
        task.mark_completed()
        task_repository.create(task)

        result = await get_tasks_tool.execute(query_type="completed")

        assert result.success is True
        assert len(result.data) == 1

    @pytest.mark.asyncio
    async def test_execute_overdue(self, get_tasks_tool, task_repository):
        """测试查询过期任务"""
        # 创建过期任务
        task = Task(
            title="过期任务",
            due_date=date.today() - timedelta(days=1),
            status=TaskStatus.TODO,
        )
        task_repository.create(task)

        result = await get_tasks_tool.execute(query_type="overdue")

        assert result.success is True
        assert len(result.data) == 1
        assert result.data[0].title == "过期任务"

    @pytest.mark.asyncio
    async def test_execute_search(self, get_tasks_tool, task_repository):
        """测试搜索任务"""
        # 创建多个任务
        task1 = Task(title="写报告")
        task2 = Task(title="开会")
        task_repository.create(task1)
        task_repository.create(task2)

        result = await get_tasks_tool.execute(query_type="search", search_query="报告")

        assert result.success is True
        assert len(result.data) == 1
        assert result.data[0].title == "写报告"

    @pytest.mark.asyncio
    async def test_execute_search_empty_query(self, get_tasks_tool):
        """测试搜索关键词为空"""
        result = await get_tasks_tool.execute(query_type="search", search_query="")

        assert result.success is False
        assert result.error == "MISSING_SEARCH_QUERY"

    @pytest.mark.asyncio
    async def test_execute_all(self, get_tasks_tool, task_repository):
        """测试查询所有任务"""
        # 创建多个任务
        for i in range(3):
            task = Task(title=f"任务{i}", status=TaskStatus.TODO)
            task_repository.create(task)

        result = await get_tasks_tool.execute(query_type="all")

        assert result.success is True
        assert len(result.data) == 3

    @pytest.mark.asyncio
    async def test_execute_all_excludes_cancelled(self, get_tasks_tool, task_repository):
        """测试查询所有任务排除已取消"""
        # 创建任务
        task1 = Task(title="正常任务", status=TaskStatus.TODO)
        task2 = Task(title="已取消任务", status=TaskStatus.CANCELLED)
        task_repository.create(task1)
        task_repository.create(task2)

        result = await get_tasks_tool.execute(query_type="all")

        assert result.success is True
        assert len(result.data) == 1
        assert result.data[0].title == "正常任务"

    @pytest.mark.asyncio
    async def test_execute_default_to_todo(self, get_tasks_tool):
        """测试默认查询待办"""
        result = await get_tasks_tool.execute()

        assert result.success is True
        assert result.metadata["query_type"] == "todo"

    @pytest.mark.asyncio
    async def test_execute_tasks_sorted_by_priority(self, get_tasks_tool, task_repository):
        """测试任务按优先级排序"""
        # 创建不同优先级的任务
        task1 = Task(title="低优先级", priority=TaskPriority.LOW)
        task2 = Task(title="紧急", priority=TaskPriority.URGENT)
        task3 = Task(title="高优先级", priority=TaskPriority.HIGH)
        task_repository.create(task1)
        task_repository.create(task2)
        task_repository.create(task3)

        result = await get_tasks_tool.execute(query_type="all")

        assert result.success is True
        assert len(result.data) == 3
        # 应该按优先级排序
        assert result.data[0].title == "紧急"
        assert result.data[1].title == "高优先级"
        assert result.data[2].title == "低优先级"

    @pytest.mark.asyncio
    async def test_execute_tasks_sorted_by_due_date(self, get_tasks_tool, task_repository):
        """测试任务按截止日期排序（同优先级）"""
        # 创建同优先级不同截止日期的任务
        task1 = Task(
            title="后截止",
            priority=TaskPriority.MEDIUM,
            due_date=date.today() + timedelta(days=10),
        )
        task2 = Task(
            title="先截止",
            priority=TaskPriority.MEDIUM,
            due_date=date.today() + timedelta(days=1),
        )
        task_repository.create(task1)
        task_repository.create(task2)

        result = await get_tasks_tool.execute(query_type="all")

        assert result.success is True
        assert len(result.data) == 2
        # 应该按截止日期排序
        assert result.data[0].title == "先截止"
        assert result.data[1].title == "后截止"

    @pytest.mark.asyncio
    async def test_execute_detects_overdue_tasks(self, get_tasks_tool, task_repository):
        """测试查询待办任务时检测过期任务"""
        from datetime import date, timedelta
        
        # 创建一个过期任务和一个未过期任务
        overdue_task = Task(
            title="过期任务",
            due_date=date.today() - timedelta(days=1),
            status=TaskStatus.TODO,
        )
        normal_task = Task(
            title="正常任务",
            due_date=date.today() + timedelta(days=1),
            status=TaskStatus.TODO,
        )
        task_repository.create(overdue_task)
        task_repository.create(normal_task)

        result = await get_tasks_tool.execute(query_type="todo")

        assert result.success is True
        assert len(result.data) == 2
        # 应该检测到过期任务
        assert result.metadata.get("has_overdue") is True
        assert result.metadata.get("overdue_count") == 1
        assert overdue_task.id in result.metadata.get("overdue_task_ids", [])

    @pytest.mark.asyncio
    async def test_execute_no_overdue_when_querying_overdue(self, get_tasks_tool, task_repository):
        """测试查询过期任务时不再重复标记"""
        from datetime import date, timedelta
        
        overdue_task = Task(
            title="过期任务",
            due_date=date.today() - timedelta(days=1),
            status=TaskStatus.TODO,
        )
        task_repository.create(overdue_task)

        result = await get_tasks_tool.execute(query_type="overdue")

        assert result.success is True
        # 查询过期任务时，不应该在 metadata 中再次标记
        assert result.metadata.get("has_overdue") is None


# ============================================================================
# UpdateTaskTool 测试
# ============================================================================

class TestUpdateTaskTool:
    """UpdateTaskTool 测试类"""

    def test_name(self, update_task_tool):
        assert update_task_tool.name == "update_task"

    def test_get_definition(self, update_task_tool):
        definition = update_task_tool.get_definition()
        assert isinstance(definition, ToolDefinition)
        assert definition.name == "update_task"
        param_names = [p.name for p in definition.parameters]
        assert "task_id" in param_names
        assert "status" in param_names

    @pytest.mark.asyncio
    async def test_mark_completed(self, update_task_tool, task_repository):
        """测试标记任务为已完成"""
        task = Task(title="待完成任务", status=TaskStatus.TODO)
        task_repository.create(task)

        result = await update_task_tool.execute(
            task_id=task.id, status="completed"
        )

        assert result.success is True
        assert result.metadata["old_status"] == "todo"
        assert result.metadata["new_status"] == "completed"

        updated = task_repository.get(task.id)
        assert updated.status == TaskStatus.COMPLETED
        assert updated.completed_at is not None

    @pytest.mark.asyncio
    async def test_mark_cancelled(self, update_task_tool, task_repository):
        """测试取消任务"""
        task = Task(title="待取消任务")
        task_repository.create(task)

        result = await update_task_tool.execute(
            task_id=task.id, status="cancelled"
        )

        assert result.success is True
        assert result.metadata["new_status"] == "cancelled"

        updated = task_repository.get(task.id)
        assert updated.status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_mark_in_progress(self, update_task_tool, task_repository):
        """测试标记为进行中"""
        task = Task(title="待开始任务")
        task_repository.create(task)

        result = await update_task_tool.execute(
            task_id=task.id, status="in_progress"
        )

        assert result.success is True
        updated = task_repository.get(task.id)
        assert updated.status == TaskStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_revert_to_todo(self, update_task_tool, task_repository):
        """测试重新设为待办"""
        task = Task(title="进行中任务", status=TaskStatus.IN_PROGRESS)
        task_repository.create(task)

        result = await update_task_tool.execute(
            task_id=task.id, status="todo"
        )

        assert result.success is True
        updated = task_repository.get(task.id)
        assert updated.status == TaskStatus.TODO

    @pytest.mark.asyncio
    async def test_missing_task_id(self, update_task_tool):
        """测试缺少任务 ID"""
        result = await update_task_tool.execute(status="completed")
        assert result.success is False
        assert result.error == "MISSING_TASK_ID"

    @pytest.mark.asyncio
    async def test_missing_status(self, update_task_tool, task_repository):
        """测试缺少更新参数（既没有 status 也没有 due_date）"""
        task = Task(title="任务")
        task_repository.create(task)

        result = await update_task_tool.execute(task_id=task.id)
        assert result.success is False
        assert result.error == "MISSING_UPDATE_PARAM"

    @pytest.mark.asyncio
    async def test_invalid_status(self, update_task_tool, task_repository):
        """测试无效状态值"""
        task = Task(title="任务")
        task_repository.create(task)

        result = await update_task_tool.execute(
            task_id=task.id, status="invalid_status"
        )
        assert result.success is False
        assert result.error == "INVALID_STATUS"

    @pytest.mark.asyncio
    async def test_task_not_found(self, update_task_tool):
        """测试任务不存在"""
        result = await update_task_tool.execute(
            task_id="nonexistent", status="completed"
        )
        assert result.success is False
        assert result.error == "TASK_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_completed_task_not_deleted(self, update_task_tool, task_repository):
        """测试标记完成后任务仍然存在（不是删除）"""
        task = Task(title="重要任务")
        task_repository.create(task)

        await update_task_tool.execute(task_id=task.id, status="completed")

        still_exists = task_repository.get(task.id)
        assert still_exists is not None
        assert still_exists.title == "重要任务"
        assert still_exists.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_update_due_date(self, update_task_tool, task_repository):
        """测试更新任务的截止日期"""
        from datetime import date
        task = Task(title="任务", due_date=date(2026, 2, 20))
        task_repository.create(task)

        result = await update_task_tool.execute(
            task_id=task.id, due_date="2026-02-25"
        )
        assert result.success is True
        assert "截止日期" in result.message

        updated_task = task_repository.get(task.id)
        assert updated_task.due_date == date(2026, 2, 25)

    @pytest.mark.asyncio
    async def test_update_status_and_due_date(self, update_task_tool, task_repository):
        """测试同时更新状态和截止日期"""
        from datetime import date
        task = Task(title="任务", due_date=date(2026, 2, 20))
        task_repository.create(task)

        result = await update_task_tool.execute(
            task_id=task.id, status="in_progress", due_date="2026-02-25"
        )
        assert result.success is True

        updated_task = task_repository.get(task.id)
        assert updated_task.status == TaskStatus.IN_PROGRESS
        assert updated_task.due_date == date(2026, 2, 25)

    @pytest.mark.asyncio
    async def test_update_due_date_invalid_format(self, update_task_tool, task_repository):
        """测试无效的日期格式"""
        task = Task(title="任务")
        task_repository.create(task)

        result = await update_task_tool.execute(
            task_id=task.id, due_date="2026/02/25"
        )
        assert result.success is False
        assert result.error == "INVALID_DATE_FORMAT"


# ============================================================================
# UpdateEventTool 测试
# ============================================================================

class TestUpdateEventTool:
    """UpdateEventTool 测试类 - 修改日程（状态、标题、时间等）"""

    def test_name(self, update_event_tool):
        assert update_event_tool.name == "update_event"

    def test_get_definition(self, update_event_tool):
        definition = update_event_tool.get_definition()
        assert isinstance(definition, ToolDefinition)
        assert definition.name == "update_event"
        param_names = [p.name for p in definition.parameters]
        assert "event_id" in param_names
        assert "status" in param_names
        assert "title" in param_names
        assert "start_time" in param_names
        assert "end_time" in param_names

    @pytest.mark.asyncio
    async def test_update_status_completed(self, update_event_tool, event_repository):
        """测试标记事件为已完成"""
        today = date.today()
        event = Event(
            title="待完成事件",
            start_time=datetime.combine(today, datetime.min.time().replace(hour=10)),
            end_time=datetime.combine(today, datetime.min.time().replace(hour=11)),
        )
        event_repository.create(event)

        result = await update_event_tool.execute(event_id=event.id, status="completed")

        assert result.success is True
        assert result.metadata["new_status"] == "completed"
        updated = event_repository.get(event.id)
        assert updated.status == EventStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_update_title(self, update_event_tool, event_repository):
        """测试修改事件标题"""
        today = date.today()
        event = Event(
            title="原标题",
            start_time=datetime.combine(today, datetime.min.time().replace(hour=10)),
            end_time=datetime.combine(today, datetime.min.time().replace(hour=11)),
        )
        event_repository.create(event)

        result = await update_event_tool.execute(event_id=event.id, title="新标题")

        assert result.success is True
        assert "标题" in result.message
        updated = event_repository.get(event.id)
        assert updated.title == "新标题"

    @pytest.mark.asyncio
    async def test_update_time(self, update_event_tool, event_repository):
        """测试修改事件时间"""
        today = date.today()
        event = Event(
            title="会议",
            start_time=datetime.combine(today, datetime.min.time().replace(hour=10)),
            end_time=datetime.combine(today, datetime.min.time().replace(hour=11)),
        )
        event_repository.create(event)

        new_start = f"{today}T16:00:00"
        new_end = f"{today}T17:00:00"
        result = await update_event_tool.execute(
            event_id=event.id, start_time=new_start, end_time=new_end
        )

        assert result.success is True
        updated = event_repository.get(event.id)
        assert updated.start_time.isoformat().startswith(str(today))
        assert updated.end_time.hour == 17

    @pytest.mark.asyncio
    async def test_update_location(self, update_event_tool, event_repository):
        """测试修改事件地点"""
        today = date.today()
        event = Event(
            title="会议",
            start_time=datetime.combine(today, datetime.min.time().replace(hour=10)),
            end_time=datetime.combine(today, datetime.min.time().replace(hour=11)),
            location="会议室A",
        )
        event_repository.create(event)

        result = await update_event_tool.execute(event_id=event.id, location="会议室B")

        assert result.success is True
        updated = event_repository.get(event.id)
        assert updated.location == "会议室B"

    @pytest.mark.asyncio
    async def test_update_multiple_fields(self, update_event_tool, event_repository):
        """测试同时修改多个字段"""
        today = date.today()
        event = Event(
            title="原会议",
            start_time=datetime.combine(today, datetime.min.time().replace(hour=10)),
            end_time=datetime.combine(today, datetime.min.time().replace(hour=11)),
            location="A",
        )
        event_repository.create(event)

        result = await update_event_tool.execute(
            event_id=event.id,
            title="新会议",
            location="B",
            priority="high",
        )

        assert result.success is True
        updated = event_repository.get(event.id)
        assert updated.title == "新会议"
        assert updated.location == "B"
        assert updated.priority == EventPriority.HIGH

    @pytest.mark.asyncio
    async def test_missing_event_id(self, update_event_tool):
        result = await update_event_tool.execute(status="completed")
        assert result.success is False
        assert result.error == "MISSING_EVENT_ID"

    @pytest.mark.asyncio
    async def test_missing_update_param(self, update_event_tool, event_repository):
        event = Event(
            title="测试",
            start_time=datetime(2026, 2, 18, 10, 0),
            end_time=datetime(2026, 2, 18, 11, 0),
        )
        event_repository.create(event)

        result = await update_event_tool.execute(event_id=event.id)
        assert result.success is False
        assert result.error == "MISSING_UPDATE_PARAM"

    @pytest.mark.asyncio
    async def test_event_not_found(self, update_event_tool):
        result = await update_event_tool.execute(event_id="nonexistent", status="completed")
        assert result.success is False
        assert result.error == "EVENT_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_end_before_start(self, update_event_tool, event_repository):
        today = date.today()
        event = Event(
            title="会议",
            start_time=datetime.combine(today, datetime.min.time().replace(hour=10)),
            end_time=datetime.combine(today, datetime.min.time().replace(hour=11)),
        )
        event_repository.create(event)

        result = await update_event_tool.execute(
            event_id=event.id,
            start_time=f"{today}T12:00:00",
            end_time=f"{today}T11:00:00",
        )
        assert result.success is False
        assert result.error == "INVALID_TIME_RANGE"


# ============================================================================
# DeleteScheduleDataTool 测试
# ============================================================================

class TestDeleteScheduleDataTool:
    """DeleteScheduleDataTool 测试类"""

    def test_name(self, delete_tool):
        """测试工具名称"""
        assert delete_tool.name == "delete_schedule_data"

    def test_get_definition(self, delete_tool):
        """测试获取工具定义"""
        definition = delete_tool.get_definition()
        assert isinstance(definition, ToolDefinition)
        assert definition.name == "delete_schedule_data"
        param_names = [p.name for p in definition.parameters]
        assert "resource_type" in param_names
        assert "confirm" in param_names

    @pytest.mark.asyncio
    async def test_delete_single_task_success(self, delete_tool, task_repository):
        """测试单条删除任务"""
        task = Task(title="待删除任务")
        task_repository.create(task)

        result = await delete_tool.execute(
            resource_type="task",
            target_ids=[task.id],
            confirm=True,
        )

        assert result.success is True
        assert result.metadata["mode"] == "single"
        assert task_repository.get(task.id) is None

    @pytest.mark.asyncio
    async def test_delete_requires_confirmation(self, delete_tool, task_repository):
        """测试删除必须确认"""
        task = Task(title="待删除任务")
        task_repository.create(task)

        result = await delete_tool.execute(
            resource_type="task",
            target_ids=[task.id],
            confirm=False,
        )

        assert result.success is False
        assert result.error == "CONFIRMATION_REQUIRED"
        assert task_repository.get(task.id) is not None

    @pytest.mark.asyncio
    async def test_batch_delete_success_with_confirm(self, delete_tool, task_repository):
        """测试批量删除（用户确认后）"""
        task1 = Task(title="任务1")
        task2 = Task(title="任务2")
        task_repository.create(task1)
        task_repository.create(task2)

        result = await delete_tool.execute(
            resource_type="task",
            target_ids=[task1.id, task2.id],
            confirm=True,
        )

        assert result.success is True
        assert result.metadata["mode"] == "batch"
        assert result.metadata["deleted_count"] == 2
        assert task_repository.get(task1.id) is None
        assert task_repository.get(task2.id) is None

    @pytest.mark.asyncio
    async def test_delete_all_success(self, delete_tool, event_repository):
        """测试全量删除（用户确认后）"""
        today = date.today()
        for i in range(2):
            event = Event(
                title=f"待删除事件{i}",
                start_time=datetime.combine(today, datetime.min.time().replace(hour=9 + i)),
                end_time=datetime.combine(today, datetime.min.time().replace(hour=10 + i)),
            )
            event_repository.create(event)

        result = await delete_tool.execute(
            resource_type="event",
            delete_all=True,
            confirm=True,
        )

        assert result.success is True
        assert result.metadata["mode"] == "all"
        assert event_repository.get_all() == []


# ============================================================================
# 工具集成测试
# ============================================================================

class TestStorageToolsIntegration:
    """存储工具集成测试"""

    @pytest.mark.asyncio
    async def test_add_and_get_event(self, add_event_tool, get_events_tool, event_repository):
        """测试添加和获取事件"""
        tomorrow = date.today() + timedelta(days=1)

        # 添加事件
        add_result = await add_event_tool.execute(
            title="集成测试事件",
            start_time=f"{tomorrow}T10:00:00",
            end_time=f"{tomorrow}T11:00:00",
        )

        assert add_result.success is True

        # 获取事件
        get_result = await get_events_tool.execute(query_type="upcoming", days=7)

        assert get_result.success is True
        assert len(get_result.data) == 1
        assert get_result.data[0].title == "集成测试事件"

    @pytest.mark.asyncio
    async def test_add_and_get_task(self, add_task_tool, get_tasks_tool, task_repository):
        """测试添加和获取任务"""
        # 添加任务
        add_result = await add_task_tool.execute(
            title="集成测试任务",
            estimated_minutes=90,
            priority="high",
        )

        assert add_result.success is True

        # 获取任务
        get_result = await get_tasks_tool.execute(query_type="todo")

        assert get_result.success is True
        assert len(get_result.data) == 1
        assert get_result.data[0].title == "集成测试任务"

    @pytest.mark.asyncio
    async def test_openai_tool_format(self, add_event_tool, add_task_tool):
        """测试 OpenAI 工具格式转换"""
        event_def = add_event_tool.to_openai_tool()
        task_def = add_task_tool.to_openai_tool()

        assert event_def["type"] == "function"
        assert event_def["function"]["name"] == "add_event"
        assert "parameters" in event_def["function"]

        assert task_def["type"] == "function"
        assert task_def["function"]["name"] == "add_task"
        assert "parameters" in task_def["function"]
