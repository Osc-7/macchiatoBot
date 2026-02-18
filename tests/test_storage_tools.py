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
