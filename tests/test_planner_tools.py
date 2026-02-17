"""
规划器工具测试 - 测试 GetFreeSlotsTool 和 PlanTasksTool
"""

import pytest
from datetime import datetime, date, timedelta
from pathlib import Path
import tempfile
import os

from schedule_agent.core.tools.planner_tools import GetFreeSlotsTool, PlanTasksTool
from schedule_agent.core.tools.base import ToolResult
from schedule_agent.storage.json_repository import EventRepository, TaskRepository
from schedule_agent.models import Event, Task, EventStatus, TaskStatus, EventPriority, TaskPriority


# === Fixtures ===

@pytest.fixture
def temp_dir():
    """创建临时目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def event_repo(temp_dir):
    """创建临时事件仓库"""
    file_path = os.path.join(temp_dir, "events.json")
    return EventRepository(file_path)


@pytest.fixture
def task_repo(temp_dir):
    """创建临时任务仓库"""
    file_path = os.path.join(temp_dir, "tasks.json")
    return TaskRepository(file_path)


@pytest.fixture
def get_free_slots_tool(event_repo):
    """创建获取空闲时间段工具"""
    return GetFreeSlotsTool(
        event_repository=event_repo,
        sleep_start_hour=23,
        sleep_end_hour=8,
    )


@pytest.fixture
def plan_tasks_tool(event_repo, task_repo):
    """创建规划任务工具"""
    return PlanTasksTool(
        event_repository=event_repo,
        task_repository=task_repo,
        sleep_start_hour=23,
        sleep_end_hour=8,
    )


# === GetFreeSlotsTool 测试 ===

class TestGetFreeSlotsToolDefinition:
    """测试 GetFreeSlotsTool 的工具定义"""

    def test_tool_name(self, get_free_slots_tool):
        """测试工具名称"""
        assert get_free_slots_tool.name == "get_free_slots"

    def test_tool_definition(self, get_free_slots_tool):
        """测试工具定义"""
        definition = get_free_slots_tool.get_definition()
        assert definition.name == "get_free_slots"
        assert "空闲时间段" in definition.description
        assert len(definition.parameters) >= 2
        assert len(definition.examples) >= 2

    def test_tool_has_required_parameters(self, get_free_slots_tool):
        """测试工具是否有必要的参数"""
        definition = get_free_slots_tool.get_definition()
        param_names = [p.name for p in definition.parameters]
        assert "date" in param_names
        assert "days" in param_names
        assert "min_duration" in param_names


class TestGetFreeSlotsToolExecute:
    """测试 GetFreeSlotsTool 的执行"""

    @pytest.mark.asyncio
    async def test_execute_with_no_events(self, get_free_slots_tool):
        """测试没有事件时的空闲时间"""
        result = await get_free_slots_tool.execute(
            date="2026-02-17",
            days=1,
        )

        assert result.success is True
        assert "free_slots" in result.data
        assert result.data["total_count"] > 0
        # 应该有睡眠时间外的空闲时间
        assert result.data["total_minutes"] > 0

    @pytest.mark.asyncio
    async def test_execute_with_default_date(self, get_free_slots_tool):
        """测试使用默认日期（今天）"""
        result = await get_free_slots_tool.execute()

        assert result.success is True
        assert "free_slots" in result.data

    @pytest.mark.asyncio
    async def test_execute_with_invalid_date_format(self, get_free_slots_tool):
        """测试无效日期格式"""
        result = await get_free_slots_tool.execute(date="2026/02/17")

        assert result.success is False
        assert result.error == "INVALID_DATE_FORMAT"

    @pytest.mark.asyncio
    async def test_execute_with_min_duration_filter(self, get_free_slots_tool):
        """测试最小时长过滤"""
        # 获取所有空闲时间段
        result_all = await get_free_slots_tool.execute(
            date="2026-02-17",
            days=1,
        )

        # 获取至少60分钟的空闲时间段
        result_filtered = await get_free_slots_tool.execute(
            date="2026-02-17",
            days=1,
            min_duration=60,
        )

        assert result_all.success is True
        assert result_filtered.success is True

        # 过滤后的时间段应该少于或等于全部
        assert result_filtered.data["total_count"] <= result_all.data["total_count"]

    @pytest.mark.asyncio
    async def test_execute_multiple_days(self, get_free_slots_tool):
        """测试多天查询"""
        result = await get_free_slots_tool.execute(
            date="2026-02-17",
            days=3,
        )

        assert result.success is True
        assert result.data["query_range"]["days"] == 3

    @pytest.mark.asyncio
    async def test_execute_with_events(self, event_repo):
        """测试有事件时的空闲时间计算"""
        # 创建一个上午的事件
        today = date(2026, 2, 17)
        event = Event(
            title="测试会议",
            start_time=datetime(2026, 2, 17, 9, 0),
            end_time=datetime(2026, 2, 17, 10, 0),
        )
        event_repo.create(event)

        tool = GetFreeSlotsTool(event_repository=event_repo)
        result = await tool.execute(date="2026-02-17", days=1)

        assert result.success is True
        # 检查9点到10点的时间段不在空闲列表中
        for slot in result.data["free_slots"]:
            # TimeSlot 对象直接访问属性
            assert not (slot.start_time.hour == 9 or slot.end_time.hour == 10)


class TestGetFreeSlotsToolInternal:
    """测试 GetFreeSlotsTool 的内部方法"""

    def test_create_sleep_slots(self, get_free_slots_tool):
        """测试创建睡眠时间段"""
        target_date = date(2026, 2, 17)
        sleep_slots = get_free_slots_tool._create_sleep_slots(target_date)

        assert len(sleep_slots) == 2
        # 第一个是前一天晚上到当天早上
        assert sleep_slots[0].start_time.hour == 23
        assert sleep_slots[0].end_time.hour == 8
        # 第二个是当天晚上
        assert sleep_slots[1].start_time.hour == 23

    def test_get_busy_slots(self, event_repo):
        """测试获取忙碌时间段"""
        # 创建事件
        event1 = Event(
            title="事件1",
            start_time=datetime(2026, 2, 17, 9, 0),
            end_time=datetime(2026, 2, 17, 10, 0),
        )
        event2 = Event(
            title="事件2",
            start_time=datetime(2026, 2, 17, 14, 0),
            end_time=datetime(2026, 2, 17, 15, 0),
        )
        event_repo.create(event1)
        event_repo.create(event2)

        tool = GetFreeSlotsTool(event_repository=event_repo)
        busy_slots = tool._get_busy_slots(date(2026, 2, 17), date(2026, 2, 17))

        assert len(busy_slots) == 2

    def test_calculate_free_slots(self, get_free_slots_tool):
        """测试计算空闲时间段"""
        start_dt = datetime(2026, 2, 17, 8, 0)
        end_dt = datetime(2026, 2, 17, 20, 0)

        busy_slots = [
            type('TimeSlot', (), {
                'start_time': datetime(2026, 2, 17, 10, 0),
                'end_time': datetime(2026, 2, 17, 11, 0),
            })()
        ]
        sleep_slots = []

        free_slots = get_free_slots_tool._calculate_free_slots(
            start_dt, end_dt, busy_slots, sleep_slots
        )

        # 应该有2个空闲时间段：8-10和11-20
        assert len(free_slots) == 2


# === PlanTasksTool 测试 ===

class TestPlanTasksToolDefinition:
    """测试 PlanTasksTool 的工具定义"""

    def test_tool_name(self, plan_tasks_tool):
        """测试工具名称"""
        assert plan_tasks_tool.name == "plan_tasks"

    def test_tool_definition(self, plan_tasks_tool):
        """测试工具定义"""
        definition = plan_tasks_tool.get_definition()
        assert definition.name == "plan_tasks"
        assert "自动" in definition.description
        assert len(definition.parameters) >= 3
        assert len(definition.examples) >= 2

    def test_tool_has_required_parameters(self, plan_tasks_tool):
        """测试工具是否有必要的参数"""
        definition = plan_tasks_tool.get_definition()
        param_names = [p.name for p in definition.parameters]
        assert "days" in param_names
        assert "max_tasks" in param_names
        assert "task_ids" in param_names
        assert "prefer_morning" in param_names


class TestPlanTasksToolExecute:
    """测试 PlanTasksTool 的执行"""

    @pytest.mark.asyncio
    async def test_execute_with_no_tasks(self, plan_tasks_tool):
        """测试没有任务时的规划"""
        result = await plan_tasks_tool.execute(days=3, max_tasks=5)

        assert result.success is True
        assert result.data["planned_tasks"] == []
        assert result.data["unplanned_tasks"] == []

    @pytest.mark.asyncio
    async def test_execute_with_tasks(self, task_repo):
        """测试有任务时的规划"""
        # 创建任务
        task = Task(
            title="测试任务",
            estimated_minutes=60,
            priority=TaskPriority.HIGH,
        )
        task_repo.create(task)

        tool = PlanTasksTool(task_repository=task_repo)
        result = await tool.execute(days=3, max_tasks=5)

        assert result.success is True
        assert len(result.data["planned_tasks"]) == 1
        assert result.data["summary"]["planned"] == 1

    @pytest.mark.asyncio
    async def test_execute_with_specific_task_ids(self, task_repo):
        """测试指定任务ID的规划"""
        # 创建多个任务
        task1 = Task(title="任务1", estimated_minutes=30)
        task2 = Task(title="任务2", estimated_minutes=60)
        task_repo.create(task1)
        task_repo.create(task2)

        tool = PlanTasksTool(task_repository=task_repo)
        result = await tool.execute(
            days=3,
            task_ids=[task1.id],
        )

        assert result.success is True
        # 只规划指定的任务
        assert len(result.data["planned_tasks"]) <= 1

    @pytest.mark.asyncio
    async def test_execute_respects_max_tasks(self, task_repo):
        """测试最大任务数量限制"""
        # 创建5个任务
        for i in range(5):
            task = Task(title=f"任务{i}", estimated_minutes=30)
            task_repo.create(task)

        tool = PlanTasksTool(task_repository=task_repo)
        result = await tool.execute(days=1, max_tasks=2)

        assert result.success is True
        # 最多规划2个
        assert len(result.data["planned_tasks"]) <= 2

    @pytest.mark.asyncio
    async def test_execute_creates_events(self, task_repo, event_repo):
        """测试规划会创建事件"""
        task = Task(
            title="测试任务",
            estimated_minutes=60,
        )
        task_repo.create(task)

        tool = PlanTasksTool(
            event_repository=event_repo,
            task_repository=task_repo,
        )
        result = await tool.execute(days=3)

        if result.data["planned_tasks"]:
            # 检查事件是否创建
            planned = result.data["planned_tasks"][0]
            event_id = planned["event_id"]
            event = event_repo.get(event_id)
            assert event is not None
            assert "[任务]" in event.title

    @pytest.mark.asyncio
    async def test_execute_updates_task_status(self, task_repo, event_repo):
        """测试规划会更新任务状态"""
        task = Task(
            title="测试任务",
            estimated_minutes=60,
        )
        task_repo.create(task)

        tool = PlanTasksTool(
            event_repository=event_repo,
            task_repository=task_repo,
        )
        result = await tool.execute(days=3)

        if result.data["planned_tasks"]:
            # 检查任务状态是否更新
            task_id = result.data["planned_tasks"][0]["task_id"]
            updated_task = task_repo.get(task_id)
            assert updated_task.status == TaskStatus.IN_PROGRESS
            assert updated_task.is_scheduled is True

    @pytest.mark.asyncio
    async def test_execute_prioritizes_by_priority(self, task_repo):
        """测试按优先级排序"""
        # 创建不同优先级的任务
        low_task = Task(title="低优先级", priority=TaskPriority.LOW, estimated_minutes=30)
        high_task = Task(title="高优先级", priority=TaskPriority.HIGH, estimated_minutes=30)
        urgent_task = Task(title="紧急", priority=TaskPriority.URGENT, estimated_minutes=30)
        task_repo.create(low_task)
        task_repo.create(high_task)
        task_repo.create(urgent_task)

        tool = PlanTasksTool(task_repository=task_repo)
        tasks = tool._get_tasks_to_plan(None, 3)

        # 紧急任务应该排在第一位
        assert tasks[0].priority == TaskPriority.URGENT
        assert tasks[1].priority == TaskPriority.HIGH
        assert tasks[2].priority == TaskPriority.LOW

    @pytest.mark.asyncio
    async def test_execute_prioritizes_by_due_date(self, task_repo):
        """测试按截止日期排序"""
        # 创建相同优先级但不同截止日期的任务
        task1 = Task(
            title="后截止",
            priority=TaskPriority.MEDIUM,
            estimated_minutes=30,
            due_date=date.today() + timedelta(days=7),
        )
        task2 = Task(
            title="先截止",
            priority=TaskPriority.MEDIUM,
            estimated_minutes=30,
            due_date=date.today() + timedelta(days=1),
        )
        task_repo.create(task1)
        task_repo.create(task2)

        tool = PlanTasksTool(task_repository=task_repo)
        tasks = tool._get_tasks_to_plan(None, 2)

        # 先截止的任务应该排在前面
        assert tasks[0].due_date < tasks[1].due_date


class TestPlanTasksToolInternal:
    """测试 PlanTasksTool 的内部方法"""

    def test_get_tasks_to_plan_with_ids(self, task_repo):
        """测试获取指定任务"""
        task1 = Task(title="任务1")
        task2 = Task(title="任务2")
        task_repo.create(task1)
        task_repo.create(task2)

        tool = PlanTasksTool(task_repository=task_repo)
        tasks = tool._get_tasks_to_plan([task1.id], 5)

        assert len(tasks) == 1
        assert tasks[0].id == task1.id

    def test_get_tasks_to_plan_without_ids(self, task_repo):
        """测试自动获取待办任务"""
        task1 = Task(title="待办1", status=TaskStatus.TODO)
        task2 = Task(title="已完成", status=TaskStatus.COMPLETED)
        task3 = Task(title="待办2", status=TaskStatus.TODO)
        task_repo.create(task1)
        task_repo.create(task2)
        task_repo.create(task3)

        tool = PlanTasksTool(task_repository=task_repo)
        tasks = tool._get_tasks_to_plan(None, 10)

        # 只应该获取待办任务
        assert len(tasks) == 2

    def test_find_suitable_slot(self, plan_tasks_tool):
        """测试查找合适的空闲时间段"""
        from schedule_agent.models import TimeSlot, SlotType

        task = Task(title="测试", estimated_minutes=60)

        # 创建空闲时间段列表
        slots = [
            TimeSlot(
                start_time=datetime(2026, 2, 17, 9, 0),
                end_time=datetime(2026, 2, 17, 10, 0),  # 只有60分钟
                slot_type=SlotType.FREE,
            ),
            TimeSlot(
                start_time=datetime(2026, 2, 17, 14, 0),
                end_time=datetime(2026, 2, 17, 17, 0),  # 有180分钟
                slot_type=SlotType.FREE,
            ),
        ]

        slot = plan_tasks_tool._find_suitable_slot(task, slots)
        assert slot is not None
        assert slot.duration_minutes >= 60

    def test_find_suitable_slot_not_found(self, plan_tasks_tool):
        """测试找不到合适的空闲时间段"""
        from schedule_agent.models import TimeSlot, SlotType

        task = Task(title="测试", estimated_minutes=120)

        # 创建太短的空闲时间段
        slots = [
            TimeSlot(
                start_time=datetime(2026, 2, 17, 9, 0),
                end_time=datetime(2026, 2, 17, 9, 30),  # 只有30分钟
                slot_type=SlotType.FREE,
            ),
        ]

        slot = plan_tasks_tool._find_suitable_slot(task, slots)
        assert slot is None


# === 集成测试 ===

class TestPlannerToolsIntegration:
    """规划器工具集成测试"""

    @pytest.mark.asyncio
    async def test_full_planning_workflow(self, event_repo, task_repo):
        """测试完整的规划流程"""
        # 1. 创建一些事件
        event1 = Event(
            title="已有会议",
            start_time=datetime(2026, 2, 17, 10, 0),
            end_time=datetime(2026, 2, 17, 11, 0),
        )
        event_repo.create(event1)

        # 2. 创建一些任务
        task1 = Task(
            title="写报告",
            estimated_minutes=60,
            priority=TaskPriority.HIGH,
            due_date=date(2026, 2, 18),
        )
        task2 = Task(
            title="发邮件",
            estimated_minutes=30,
            priority=TaskPriority.MEDIUM,
        )
        task_repo.create(task1)
        task_repo.create(task2)

        # 3. 获取空闲时间
        free_slots_tool = GetFreeSlotsTool(event_repository=event_repo)
        slots_result = await free_slots_tool.execute(date="2026-02-17", days=1)

        assert slots_result.success is True
        assert slots_result.data["total_count"] > 0

        # 4. 规划任务
        plan_tool = PlanTasksTool(
            event_repository=event_repo,
            task_repository=task_repo,
        )
        plan_result = await plan_tool.execute(days=3, max_tasks=5)

        assert plan_result.success is True
        # 应该成功规划了一些任务
        assert plan_result.data["summary"]["planned"] >= 1

    @pytest.mark.asyncio
    async def test_avoid_conflicts_with_existing_events(self, event_repo, task_repo):
        """测试避免与现有事件冲突"""
        # 创建一个占用大部分白天的事件
        all_day_event = Event(
            title="全天培训",
            start_time=datetime(2026, 2, 17, 8, 0),
            end_time=datetime(2026, 2, 17, 18, 0),
        )
        event_repo.create(all_day_event)

        # 创建一个任务
        task = Task(
            title="测试任务",
            estimated_minutes=60,
        )
        task_repo.create(task)

        # 规划任务
        plan_tool = PlanTasksTool(
            event_repository=event_repo,
            task_repository=task_repo,
        )
        result = await plan_tool.execute(days=1, max_tasks=1)

        # 如果规划成功，检查时间不冲突
        if result.data["planned_tasks"]:
            planned = result.data["planned_tasks"][0]
            scheduled_start = datetime.fromisoformat(planned["scheduled_start"])
            scheduled_end = datetime.fromisoformat(planned["scheduled_end"])

            # 应该不在 8:00-18:00 之间
            assert not (scheduled_start.hour >= 8 and scheduled_end.hour <= 18)

    @pytest.mark.asyncio
    async def test_sleep_time_excluded(self, event_repo):
        """测试睡眠时间被排除"""
        tool = GetFreeSlotsTool(
            event_repository=event_repo,
            sleep_start_hour=23,
            sleep_end_hour=8,
        )
        result = await tool.execute(date="2026-02-17", days=1)

        assert result.success is True

        # 检查所有空闲时间段都不在睡眠时间
        for slot in result.data["free_slots"]:
            # TimeSlot 对象直接访问属性
            start = slot.start_time
            end = slot.end_time

            # 空闲时间段不应该主要在23:00-08:00之间
            if start.hour >= 23 or start.hour < 8:
                # 如果开始时间在睡眠时间，结束时间应该在8点之后
                assert end.hour >= 8
