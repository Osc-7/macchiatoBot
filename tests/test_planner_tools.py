"""
规划器工具测试 - 测试 GetFreeSlotsTool 和 PlanTasksTool
"""

import pytest
from datetime import datetime, date, timedelta
import tempfile
import os

from schedule_agent.config import (
    PlanningConfig,
    PlanningWorkingHoursConfig,
    PlanningWeightsConfig,
)
from schedule_agent.core.tools.planner_tools import GetFreeSlotsTool, PlanTasksTool
from schedule_agent.storage.json_repository import EventRepository, TaskRepository
from schedule_agent.models import Event, Task, EventStatus, TaskStatus, TaskPriority


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def event_repo(temp_dir):
    file_path = os.path.join(temp_dir, "events.json")
    return EventRepository(file_path)


@pytest.fixture
def task_repo(temp_dir):
    file_path = os.path.join(temp_dir, "tasks.json")
    return TaskRepository(file_path)


@pytest.fixture
def planning_config():
    return PlanningConfig(
        timezone="Asia/Shanghai",
        lookahead_days=7,
        min_block_minutes=30,
        working_hours=[
            PlanningWorkingHoursConfig(weekday=i, start="09:00", end="22:00")
            for i in range(1, 8)
        ],
        weights=PlanningWeightsConfig(
            urgency=0.4,
            difficulty=0.3,
            importance=0.3,
            overdue_bonus=0.2,
        ),
    )


@pytest.fixture
def get_free_slots_tool(event_repo):
    return GetFreeSlotsTool(
        event_repository=event_repo,
        sleep_start_hour=23,
        sleep_end_hour=8,
    )


@pytest.fixture
def plan_tasks_tool(event_repo, task_repo, planning_config):
    return PlanTasksTool(
        event_repository=event_repo,
        task_repository=task_repo,
        planning_config=planning_config,
    )


class TestGetFreeSlotsTool:
    @pytest.mark.asyncio
    async def test_execute_with_no_events(self, get_free_slots_tool):
        result = await get_free_slots_tool.execute(date="2026-02-17", days=1)
        assert result.success is True
        assert result.data["total_count"] > 0

    @pytest.mark.asyncio
    async def test_execute_with_invalid_date_format(self, get_free_slots_tool):
        result = await get_free_slots_tool.execute(date="2026/02/17")
        assert result.success is False
        assert result.error == "INVALID_DATE_FORMAT"


class TestPlanTasksToolDefinition:
    def test_tool_name(self, plan_tasks_tool):
        assert plan_tasks_tool.name == "plan_tasks"

    def test_tool_has_new_parameters(self, plan_tasks_tool):
        definition = plan_tasks_tool.get_definition()
        param_names = [p.name for p in definition.parameters]
        assert "start_date" in param_names
        assert "days" in param_names
        assert "task_ids" in param_names
        assert "replace_existing_plans" in param_names
        assert "dry_run" in param_names


class TestPlanTasksToolExecute:
    @pytest.mark.asyncio
    async def test_missing_working_hours_returns_error(self, event_repo, task_repo):
        tool = PlanTasksTool(
            event_repository=event_repo,
            task_repository=task_repo,
            planning_config=PlanningConfig(working_hours=[]),
        )
        result = await tool.execute(start_date="2026-02-17", days=1)
        assert result.success is False
        assert result.error == "PLANNING_CONFIG_MISSING"

    @pytest.mark.asyncio
    async def test_execute_with_no_tasks(self, plan_tasks_tool):
        result = await plan_tasks_tool.execute(start_date="2026-02-17", days=3)
        assert result.success is True
        assert result.data["planned_items"] == []
        assert result.data["unplanned_items"] == []

    @pytest.mark.asyncio
    async def test_execute_with_tasks(self, plan_tasks_tool, task_repo):
        task = Task(
            title="测试任务",
            estimated_minutes=60,
            priority=TaskPriority.HIGH,
            due_date=date(2026, 2, 18),
            difficulty=5,
            importance=5,
        )
        task_repo.create(task)

        result = await plan_tasks_tool.execute(start_date="2026-02-17", days=3)

        assert result.success is True
        assert len(result.data["planned_items"]) == 1
        assert result.data["summary"]["planned"] == 1

        updated_task = task_repo.get(task.id)
        assert updated_task.status == TaskStatus.IN_PROGRESS
        assert updated_task.is_scheduled is True

    @pytest.mark.asyncio
    async def test_execute_dry_run_does_not_persist(self, plan_tasks_tool, task_repo, event_repo):
        task = Task(title="dry run task", estimated_minutes=60)
        task_repo.create(task)

        result = await plan_tasks_tool.execute(
            start_date="2026-02-17",
            days=1,
            dry_run=True,
        )
        assert result.success is True
        assert len(result.data["planned_items"]) == 1
        assert event_repo.get_all() == []

        refreshed = task_repo.get(task.id)
        assert refreshed.status == TaskStatus.TODO

    @pytest.mark.asyncio
    async def test_replace_existing_plans_cancels_old_blocks(self, plan_tasks_tool, event_repo, task_repo):
        old_block = Event(
            title="旧计划",
            start_time=datetime(2026, 2, 17, 9, 0),
            end_time=datetime(2026, 2, 17, 10, 0),
            source="planner",
            event_type="planned_block",
            is_blocking=True,
        )
        event_repo.create(old_block)

        task = Task(title="新任务", estimated_minutes=60)
        task_repo.create(task)

        result = await plan_tasks_tool.execute(
            start_date="2026-02-17",
            days=1,
            replace_existing_plans=True,
        )
        assert result.success is True

        updated_old = event_repo.get(old_block.id)
        assert updated_old.status == EventStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_score_prefers_urgent_important_difficult(self, plan_tasks_tool, task_repo):
        # 同一天窗口，检查排序与评分
        a = Task(
            title="普通任务",
            estimated_minutes=30,
            due_date=date(2026, 3, 5),
            difficulty=1,
            importance=1,
        )
        b = Task(
            title="高优先任务",
            estimated_minutes=30,
            due_date=date(2026, 2, 18),
            difficulty=5,
            importance=5,
        )
        task_repo.create(a)
        task_repo.create(b)

        result = await plan_tasks_tool.execute(start_date="2026-02-17", days=1, max_tasks=2)
        assert result.success is True
        planned = result.data["planned_items"]
        assert len(planned) == 2
        assert planned[0]["task_title"] == "高优先任务"
        assert planned[0]["score"] >= planned[1]["score"]

    @pytest.mark.asyncio
    async def test_avoid_conflicts_with_blocking_events(self, plan_tasks_tool, event_repo, task_repo):
        # 9-18 被 blocking 事件占用，只能排在 18 点之后
        busy = Event(
            title="全天培训",
            start_time=datetime(2026, 2, 17, 9, 0),
            end_time=datetime(2026, 2, 17, 18, 0),
            is_blocking=True,
        )
        event_repo.create(busy)
        task = Task(title="测试任务", estimated_minutes=60)
        task_repo.create(task)

        result = await plan_tasks_tool.execute(start_date="2026-02-17", days=1, max_tasks=1)
        assert result.success is True
        if result.data["planned_items"]:
            start = datetime.fromisoformat(result.data["planned_items"][0]["start_at"])
            assert start.hour >= 18


class TestPlannerIntegration:
    @pytest.mark.asyncio
    async def test_full_planning_workflow(self, event_repo, task_repo, planning_config):
        event_repo.create(
            Event(
                title="已有会议",
                start_time=datetime(2026, 2, 17, 10, 0),
                end_time=datetime(2026, 2, 17, 11, 0),
                is_blocking=True,
            )
        )

        task_repo.create(
            Task(
                title="写报告",
                estimated_minutes=60,
                priority=TaskPriority.HIGH,
                due_date=date(2026, 2, 18),
                difficulty=4,
                importance=5,
            )
        )
        task_repo.create(
            Task(
                title="发邮件",
                estimated_minutes=30,
                priority=TaskPriority.MEDIUM,
                difficulty=2,
                importance=2,
            )
        )

        plan_tool = PlanTasksTool(
            event_repository=event_repo,
            task_repository=task_repo,
            planning_config=planning_config,
        )
        plan_result = await plan_tool.execute(start_date="2026-02-17", days=3, max_tasks=5)

        assert plan_result.success is True
        assert plan_result.data["summary"]["planned"] >= 1
        assert plan_result.data["plan_run_id"] is not None

    @pytest.mark.asyncio
    async def test_break_between_tasks(self, event_repo, task_repo):
        """休息权重：任务间应有休息间隔"""
        config = PlanningConfig(
            timezone="Asia/Shanghai",
            working_hours=[
                PlanningWorkingHoursConfig(weekday=i, start="09:00", end="22:00")
                for i in range(1, 8)
            ],
            break_minutes_after_task=15,
            prefer_weekday_slots=False,  # 简化：同一天内验证休息
        )
        tool = PlanTasksTool(
            event_repository=event_repo,
            task_repository=task_repo,
            planning_config=config,
        )
        for i in range(3):
            task_repo.create(
                Task(title=f"任务{i+1}", estimated_minutes=60)
            )

        result = await tool.execute(start_date="2026-02-17", days=1, dry_run=True)
        assert result.success is True
        planned = result.data["planned_items"]
        assert len(planned) >= 2
        # 验证相邻任务间有 15 分钟休息
        for j in range(len(planned) - 1):
            end1 = datetime.fromisoformat(planned[j]["end_at"])
            start2 = datetime.fromisoformat(planned[j + 1]["start_at"])
            gap_minutes = (start2 - end1).total_seconds() / 60
            assert gap_minutes >= 15, f"任务间应有至少 15 分钟休息，实际 {gap_minutes}"

    @pytest.mark.asyncio
    async def test_prefer_weekday_slots(self, event_repo, task_repo):
        """周末权重：优先工作日排程，周末作补充"""
        config = PlanningConfig(
            timezone="Asia/Shanghai",
            working_hours=[
                PlanningWorkingHoursConfig(weekday=i, start="09:00", end="18:00")
                for i in range(1, 8)
            ],
            break_minutes_after_task=0,  # 简化验证
            prefer_weekday_slots=True,
        )
        tool = PlanTasksTool(
            event_repository=event_repo,
            task_repository=task_repo,
            planning_config=config,
        )
        task_repo.create(Task(title="唯一任务", estimated_minutes=60))

        # 2026-02-28 是周六，若优先工作日则应排在周一 03-02
        result = await tool.execute(
            start_date="2026-02-28", days=7, dry_run=True
        )
        assert result.success is True
        planned = result.data["planned_items"]
        assert len(planned) == 1
        start = datetime.fromisoformat(planned[0]["start_at"])
        # 应排在周一（3）而非周六（6）
        assert start.isoweekday() <= 5, "应优先使用工作日时段"
