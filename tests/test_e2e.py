"""
端到端场景测试 - 测试完整的用户使用场景

测试四个核心场景:
1. 基本添加 Event（日程事件）
2. 添加 Task（待办任务）
3. 混合输入和规划
4. 查看日程
"""

import os
import tempfile
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from schedule_agent.config import PlanningConfig, PlanningWorkingHoursConfig
from schedule_agent.core.tools import (
    AddEventTool,
    AddTaskTool,
    GetEventsTool,
    GetTasksTool,
    GetFreeSlotsTool,
    PlanTasksTool,
    ToolRegistry,
)
from schedule_agent.storage.json_repository import EventRepository, TaskRepository
from schedule_agent.models import (
    Event, Task, EventStatus, TaskStatus, EventPriority, TaskPriority,
    TimeSlot, SlotType,
)


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
def tool_registry(event_repository, task_repository):
    """创建工具注册表，注册所有工具"""
    registry = ToolRegistry()

    # 注册存储工具
    registry.register(AddEventTool(repository=event_repository))
    registry.register(AddTaskTool(repository=task_repository))
    registry.register(GetEventsTool(repository=event_repository))
    registry.register(GetTasksTool(repository=task_repository))

    # 注册规划器工具
    registry.register(GetFreeSlotsTool(event_repository=event_repository))
    planning_config = PlanningConfig(
        working_hours=[
            PlanningWorkingHoursConfig(weekday=i, start="09:00", end="22:00")
            for i in range(1, 8)
        ]
    )
    registry.register(PlanTasksTool(
        event_repository=event_repository,
        task_repository=task_repository,
        planning_config=planning_config,
    ))

    return registry


# ============================================================================
# 场景1: 基本添加 Event
# ============================================================================

class TestScenario1AddEvent:
    """
    场景1: 基本添加 Event

    用户故事: 作为一个用户，我想通过自然语言添加日程事件，
    系统应该正确创建事件并存储。
    """

    @pytest.mark.asyncio
    async def test_add_single_event(self, tool_registry, event_repository):
        """测试添加单个事件"""
        # 模拟用户输入: "明天下午3点到4点开会"
        tomorrow = date.today() + timedelta(days=1)
        start_time = f"{tomorrow}T15:00:00"
        end_time = f"{tomorrow}T16:00:00"

        # 执行添加事件工具
        result = await tool_registry.execute(
            "add_event",
            title="开会",
            start_time=start_time,
            end_time=end_time,
        )

        # 验证结果
        assert result.success is True
        assert result.data is not None
        assert result.data.title == "开会"
        assert "成功创建事件" in result.message

        # 验证存储
        events = event_repository.get_all()
        assert len(events) == 1
        assert events[0].title == "开会"

    @pytest.mark.asyncio
    async def test_add_event_with_details(self, tool_registry, event_repository):
        """测试添加带详细信息的事件"""
        tomorrow = date.today() + timedelta(days=1)

        result = await tool_registry.execute(
            "add_event",
            title="团队周会",
            start_time=f"{tomorrow}T10:00:00",
            end_time=f"{tomorrow}T11:30:00",
            description="讨论本周工作进展",
            location="会议室A",
            priority="high",
            tags=["工作", "会议"],
        )

        assert result.success is True
        assert result.data.description == "讨论本周工作进展"
        assert result.data.location == "会议室A"
        assert result.data.priority == EventPriority.HIGH
        assert "工作" in result.data.tags

    @pytest.mark.asyncio
    async def test_add_multiple_events(self, tool_registry, event_repository):
        """测试添加多个事件"""
        tomorrow = date.today() + timedelta(days=1)
        day_after = date.today() + timedelta(days=2)

        # 添加第一个事件
        result1 = await tool_registry.execute(
            "add_event",
            title="晨会",
            start_time=f"{tomorrow}T09:00:00",
            end_time=f"{tomorrow}T09:30:00",
        )
        assert result1.success is True

        # 添加第二个事件
        result2 = await tool_registry.execute(
            "add_event",
            title="午餐",
            start_time=f"{tomorrow}T12:00:00",
            end_time=f"{tomorrow}T13:00:00",
        )
        assert result2.success is True

        # 添加第三个事件（不同天）
        result3 = await tool_registry.execute(
            "add_event",
            title="客户拜访",
            start_time=f"{day_after}T14:00:00",
            end_time=f"{day_after}T16:00:00",
        )
        assert result3.success is True

        # 验证存储
        events = event_repository.get_all()
        assert len(events) == 3

    @pytest.mark.asyncio
    async def test_add_event_with_conflict_warning(self, tool_registry, event_repository):
        """测试添加冲突事件时返回警告"""
        tomorrow = date.today() + timedelta(days=1)

        # 添加第一个事件
        await tool_registry.execute(
            "add_event",
            title="会议A",
            start_time=f"{tomorrow}T10:00:00",
            end_time=f"{tomorrow}T12:00:00",
        )

        # 添加冲突的事件
        result = await tool_registry.execute(
            "add_event",
            title="会议B",
            start_time=f"{tomorrow}T11:00:00",
            end_time=f"{tomorrow}T13:00:00",
        )

        # 应该成功创建，但包含冲突警告
        assert result.success is True
        assert result.metadata.get("has_conflicts") is True
        assert "冲突" in result.message


# ============================================================================
# 场景2: 添加 Task
# ============================================================================

class TestScenario2AddTask:
    """
    场景2: 添加 Task

    用户故事: 作为一个用户，我想添加待办任务，
    系统应该正确创建任务并存储。
    """

    @pytest.mark.asyncio
    async def test_add_single_task(self, tool_registry, task_repository):
        """测试添加单个任务"""
        # 模拟用户输入: "添加任务：写报告，预计2小时，周五前完成"
        result = await tool_registry.execute(
            "add_task",
            title="写报告",
            estimated_minutes=120,
            due_date=(date.today() + timedelta(days=3)).isoformat(),
        )

        assert result.success is True
        assert result.data is not None
        assert result.data.title == "写报告"
        assert result.data.estimated_minutes == 120
        assert result.data.status == TaskStatus.TODO

    @pytest.mark.asyncio
    async def test_add_task_with_details(self, tool_registry, task_repository):
        """测试添加带详细信息的任务"""
        result = await tool_registry.execute(
            "add_task",
            title="完成项目文档",
            estimated_minutes=180,
            due_date=(date.today() + timedelta(days=7)).isoformat(),
            description="整理并完善项目技术文档",
            priority="high",
            tags=["工作", "文档"],
        )

        assert result.success is True
        assert result.data.description == "整理并完善项目技术文档"
        assert result.data.priority == TaskPriority.HIGH
        assert "文档" in result.data.tags

    @pytest.mark.asyncio
    async def test_add_multiple_tasks(self, tool_registry, task_repository):
        """测试添加多个任务"""
        # 添加紧急任务
        result1 = await tool_registry.execute(
            "add_task",
            title="紧急修复",
            estimated_minutes=60,
            due_date=(date.today() + timedelta(days=1)).isoformat(),
            priority="urgent",
        )
        assert result1.success is True

        # 添加普通任务
        result2 = await tool_registry.execute(
            "add_task",
            title="代码审查",
            estimated_minutes=90,
            due_date=(date.today() + timedelta(days=3)).isoformat(),
            priority="medium",
        )
        assert result2.success is True

        # 添加低优先级任务
        result3 = await tool_registry.execute(
            "add_task",
            title="整理桌面",
            estimated_minutes=30,
            priority="low",
        )
        assert result3.success is True

        # 验证存储
        tasks = task_repository.get_all()
        assert len(tasks) == 3

    @pytest.mark.asyncio
    async def test_add_task_without_due_date(self, tool_registry, task_repository):
        """测试添加没有截止日期的任务"""
        result = await tool_registry.execute(
            "add_task",
            title="学习新技术",
            estimated_minutes=120,
        )

        assert result.success is True
        assert result.data.due_date is None
        assert result.data.is_overdue is False


# ============================================================================
# 场景3: 混合输入和规划
# ============================================================================

class TestScenario3Planning:
    """
    场景3: 混合输入和规划

    用户故事: 作为一个用户，我想创建任务后让系统自动规划时间，
    系统应该将任务安排到空闲时间段。
    """

    @pytest.mark.asyncio
    async def test_get_free_slots_empty_schedule(self, tool_registry, event_repository):
        """测试获取空闲时间（空日程）"""
        tomorrow = date.today() + timedelta(days=1)

        result = await tool_registry.execute(
            "get_free_slots",
            date=tomorrow.isoformat(),
            days=1,
        )

        assert result.success is True
        assert result.data is not None
        # 空日程应该有足够的空闲时间
        assert result.data["total_minutes"] > 0

    @pytest.mark.asyncio
    async def test_get_free_slots_with_events(self, tool_registry, event_repository):
        """测试获取空闲时间（有事件）"""
        tomorrow = date.today() + timedelta(days=1)

        # 添加一个上午的事件
        await tool_registry.execute(
            "add_event",
            title="固定会议",
            start_time=f"{tomorrow}T10:00:00",
            end_time=f"{tomorrow}T12:00:00",
        )

        # 获取空闲时间
        result = await tool_registry.execute(
            "get_free_slots",
            date=tomorrow.isoformat(),
            days=1,
        )

        assert result.success is True
        # 空闲时间应该减少
        free_slots = result.data["free_slots"]
        # 10:00-12:00 不应该是空闲时间
        for slot in free_slots:
            if slot.start_time.hour >= 10 and slot.start_time.hour < 12:
                assert slot.end_time.hour <= 10 or slot.start_time.hour >= 12

    @pytest.mark.asyncio
    async def test_plan_single_task(self, tool_registry, event_repository, task_repository):
        """测试规划单个任务"""
        # 创建一个任务
        await tool_registry.execute(
            "add_task",
            title="完成报告",
            estimated_minutes=120,
            priority="high",
        )

        # 规划任务
        result = await tool_registry.execute(
            "plan_tasks",
            days=3,
            max_tasks=5,
        )

        assert result.success is True
        assert len(result.data["planned_tasks"]) == 1
        assert len(result.data["unplanned_tasks"]) == 0

        # 验证创建了事件
        events = event_repository.get_all()
        assert len(events) == 1
        assert "[任务] 完成报告" in events[0].title

    @pytest.mark.asyncio
    async def test_plan_multiple_tasks(self, tool_registry, event_repository, task_repository):
        """测试规划多个任务"""
        # 创建多个任务
        await tool_registry.execute(
            "add_task",
            title="任务A",
            estimated_minutes=60,
            priority="urgent",
        )
        await tool_registry.execute(
            "add_task",
            title="任务B",
            estimated_minutes=90,
            priority="high",
        )
        await tool_registry.execute(
            "add_task",
            title="任务C",
            estimated_minutes=120,
            priority="medium",
        )

        # 规划任务
        result = await tool_registry.execute(
            "plan_tasks",
            days=7,
            max_tasks=5,
        )

        assert result.success is True
        # 应该规划了所有任务
        assert len(result.data["planned_tasks"]) == 3

        # 验证任务按优先级排序（紧急优先）
        planned = result.data["planned_tasks"]
        assert planned[0]["task_title"] == "任务A"

    @pytest.mark.asyncio
    async def test_plan_with_existing_events(self, tool_registry, event_repository, task_repository):
        """测试在现有事件周围规划任务"""
        tomorrow = date.today() + timedelta(days=1)

        # 添加一个占据上午的事件
        await tool_registry.execute(
            "add_event",
            title="已有会议",
            start_time=f"{tomorrow}T09:00:00",
            end_time=f"{tomorrow}T12:00:00",
        )

        # 创建任务
        await tool_registry.execute(
            "add_task",
            title="需要2小时",
            estimated_minutes=120,
        )

        # 规划任务
        result = await tool_registry.execute(
            "plan_tasks",
            start_date=tomorrow.isoformat(),
            days=1,
            max_tasks=1,
        )

        assert result.success is True
        if result.data["planned_tasks"]:
            # 规划的事件不应该与现有事件冲突
            planned_event = result.data["created_events"][0]
            assert planned_event.start_time.hour >= 12 or planned_event.start_time.hour < 9

    @pytest.mark.asyncio
    async def test_plan_respects_sleep_time(self, tool_registry, event_repository, task_repository):
        """测试规划避开睡眠时间"""
        # 创建一个需要长时间的任务
        await tool_registry.execute(
            "add_task",
            title="大任务",
            estimated_minutes=480,  # 8小时
        )

        # 规划任务
        result = await tool_registry.execute(
            "plan_tasks",
            days=3,
            max_tasks=1,
        )

        assert result.success is True
        if result.data["planned_tasks"]:
            # 验证规划的时间不在睡眠时间（23:00-08:00）
            for planned in result.data["planned_tasks"]:
                start = datetime.fromisoformat(planned["scheduled_start"])
                end = datetime.fromisoformat(planned["scheduled_end"])
                # 开始时间应该在 8:00 之后
                assert start.hour >= 8
                # 结束时间应该在 23:00 之前
                assert end.hour < 23 or (end.hour == 23 and end.minute == 0)


# ============================================================================
# 场景4: 查看日程
# ============================================================================

class TestScenario4ViewSchedule:
    """
    场景4: 查看日程

    用户故事: 作为一个用户，我想查看我的日程和任务，
    系统应该返回正确和有序的结果。
    """

    @pytest.mark.asyncio
    async def test_get_events_today(self, tool_registry, event_repository):
        """测试查看今天的日程"""
        today = date.today()

        # 添加今天的事件
        await tool_registry.execute(
            "add_event",
            title="今日会议",
            start_time=f"{today}T10:00:00",
            end_time=f"{today}T11:00:00",
        )

        # 添加明天的事件
        tomorrow = today + timedelta(days=1)
        await tool_registry.execute(
            "add_event",
            title="明日会议",
            start_time=f"{tomorrow}T10:00:00",
            end_time=f"{tomorrow}T11:00:00",
        )

        # 查询今天的事件
        result = await tool_registry.execute(
            "get_events",
            query_type="today",
        )

        assert result.success is True
        assert result.data is not None
        assert len(result.data) == 1
        assert result.data[0].title == "今日会议"

    @pytest.mark.asyncio
    async def test_get_events_upcoming(self, tool_registry, event_repository):
        """测试查看即将到来的日程"""
        today = date.today()

        # 添加多个事件
        for i in range(5):
            day = today + timedelta(days=i)
            await tool_registry.execute(
                "add_event",
                title=f"事件{i}",
                start_time=f"{day}T10:00:00",
                end_time=f"{day}T11:00:00",
            )

        # 查询未来3天的事件
        result = await tool_registry.execute(
            "get_events",
            query_type="upcoming",
            days=3,
        )

        assert result.success is True
        # 应该包含今天和未来2天的事件（共3个）
        assert len(result.data) == 3

    @pytest.mark.asyncio
    async def test_get_events_search(self, tool_registry, event_repository):
        """测试搜索日程"""
        today = date.today()

        # 添加不同类型的事件
        await tool_registry.execute(
            "add_event",
            title="团队会议",
            start_time=f"{today}T10:00:00",
            end_time=f"{today}T11:00:00",
            tags=["会议"],
        )
        await tool_registry.execute(
            "add_event",
            title="项目评审会议",
            start_time=f"{today}T14:00:00",
            end_time=f"{today}T15:00:00",
            tags=["会议"],
        )
        await tool_registry.execute(
            "add_event",
            title="午餐约会",
            start_time=f"{today}T12:00:00",
            end_time=f"{today}T13:00:00",
        )

        # 搜索"会议"
        result = await tool_registry.execute(
            "get_events",
            query_type="search",
            search_query="会议",
        )

        assert result.success is True
        assert len(result.data) == 2
        for event in result.data:
            assert "会议" in event.title

    @pytest.mark.asyncio
    async def test_get_tasks_todo(self, tool_registry, task_repository):
        """测试查看待办任务"""
        # 添加待办任务
        await tool_registry.execute(
            "add_task",
            title="待办任务1",
            priority="high",
        )
        await tool_registry.execute(
            "add_task",
            title="待办任务2",
            priority="medium",
        )

        # 查询待办任务
        result = await tool_registry.execute(
            "get_tasks",
            query_type="todo",
        )

        assert result.success is True
        assert len(result.data) == 2
        # 应该按优先级排序
        assert result.data[0].priority == TaskPriority.HIGH

    @pytest.mark.asyncio
    async def test_get_tasks_overdue(self, tool_registry, task_repository):
        """测试查看过期任务"""
        # 添加一个过期任务
        yesterday = date.today() - timedelta(days=1)
        await tool_registry.execute(
            "add_task",
            title="过期任务",
            due_date=yesterday.isoformat(),
        )

        # 添加一个未来任务
        await tool_registry.execute(
            "add_task",
            title="未来任务",
            due_date=(date.today() + timedelta(days=3)).isoformat(),
        )

        # 查询过期任务
        result = await tool_registry.execute(
            "get_tasks",
            query_type="overdue",
        )

        assert result.success is True
        assert len(result.data) == 1
        assert result.data[0].title == "过期任务"

    @pytest.mark.asyncio
    async def test_get_tasks_search(self, tool_registry, task_repository):
        """测试搜索任务"""
        # 添加不同类型的任务
        await tool_registry.execute(
            "add_task",
            title="写周报",
            tags=["报告"],
        )
        await tool_registry.execute(
            "add_task",
            title="写月报",
            tags=["报告"],
        )
        await tool_registry.execute(
            "add_task",
            title="买咖啡",
        )

        # 搜索"报"
        result = await tool_registry.execute(
            "get_tasks",
            query_type="search",
            search_query="报",
        )

        assert result.success is True
        assert len(result.data) == 2


# ============================================================================
# 集成测试: 完整用户流程
# ============================================================================

class TestIntegrationFlow:
    """
    集成测试: 测试完整的用户使用流程

    模拟用户从添加任务到查看日程的完整流程。
    """

    @pytest.mark.asyncio
    async def test_complete_user_flow(self, tool_registry, event_repository, task_repository):
        """
        测试完整的用户流程:
        1. 用户添加几个固定日程
        2. 用户添加几个待办任务
        3. 系统自动规划任务
        4. 用户查看日程
        """
        today = date.today()
        tomorrow = today + timedelta(days=1)

        # 步骤1: 添加固定日程
        result = await tool_registry.execute(
            "add_event",
            title="晨会",
            start_time=f"{tomorrow}T09:00:00",
            end_time=f"{tomorrow}T09:30:00",
            priority="high",
        )
        assert result.success is True

        result = await tool_registry.execute(
            "add_event",
            title="午餐",
            start_time=f"{tomorrow}T12:00:00",
            end_time=f"{tomorrow}T13:00:00",
        )
        assert result.success is True

        # 步骤2: 添加待办任务
        result = await tool_registry.execute(
            "add_task",
            title="完成报告",
            estimated_minutes=120,
            priority="urgent",
        )
        assert result.success is True

        result = await tool_registry.execute(
            "add_task",
            title="代码审查",
            estimated_minutes=60,
            priority="high",
        )
        assert result.success is True

        # 步骤3: 规划任务
        result = await tool_registry.execute(
            "plan_tasks",
            days=3,
            max_tasks=5,
        )
        assert result.success is True
        planned_count = len(result.data["planned_tasks"])

        # 步骤4: 查看日程
        result = await tool_registry.execute(
            "get_events",
            query_type="upcoming",
            days=3,
        )
        assert result.success is True

        # 应该包含固定日程和规划的任务
        events = result.data
        event_titles = [e.title for e in events]

        # 验证固定日程存在
        assert any("晨会" in t for t in event_titles)
        assert any("午餐" in t for t in event_titles)

        # 验证规划的任务存在
        assert planned_count > 0, "应该至少规划了一个任务"

    @pytest.mark.asyncio
    async def test_multi_day_planning(self, tool_registry, event_repository, task_repository):
        """测试多天规划"""
        today = date.today()

        # 添加多个需要不同时间的任务
        for i in range(5):
            await tool_registry.execute(
                "add_task",
                title=f"任务{i}",
                estimated_minutes=120,  # 每个2小时
                priority="medium",
            )

        # 规划未来7天
        result = await tool_registry.execute(
            "plan_tasks",
            days=7,
            max_tasks=10,
        )

        assert result.success is True
        # 所有任务应该被规划
        assert len(result.data["planned_tasks"]) == 5

        # 验证创建了对应的事件
        events = event_repository.get_all()
        assert len(events) == 5
