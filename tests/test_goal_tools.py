"""Agent 目标追踪工具与 GoalStore 测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.agent.checkpoint import CoreCheckpoint, CoreCheckpointManager
from agent_core.goals.store import GoalStore
from agent_core.goals.types import GoalStatus, GoalStepStatus
from system.tools.goal_tools import (
    GoalCompleteTool,
    GoalCreateTool,
    GoalListTool,
    GoalUpdateTool,
    build_goal_tools,
)


@pytest.fixture
def store() -> GoalStore:
    return GoalStore()


@pytest.fixture
def create_tool(store: GoalStore) -> GoalCreateTool:
    return GoalCreateTool(store)


@pytest.fixture
def update_tool(store: GoalStore) -> GoalUpdateTool:
    return GoalUpdateTool(store)


@pytest.fixture
def complete_tool(store: GoalStore) -> GoalCompleteTool:
    return GoalCompleteTool(store)


@pytest.fixture
def list_tool(store: GoalStore) -> GoalListTool:
    return GoalListTool(store)


class TestGoalStore:
    def test_create_and_prompt(self, store: GoalStore) -> None:
        goal = store.create_goal(
            title="写报告",
            steps=["调研", "撰写", "校对"],
        )
        assert goal.id.startswith("goal-")
        assert len(goal.steps) == 3
        prompt = store.to_prompt_string()
        assert "写报告" in prompt
        assert "[ ]" in prompt

    def test_update_step_status(self, store: GoalStore) -> None:
        goal = store.create_goal(title="任务", steps=["第一步"])
        step_id = goal.steps[0].id
        store.update_goal(
            goal.id,
            step_id=step_id,
            step_status=GoalStepStatus.IN_PROGRESS,
        )
        updated = store.get_goal(goal.id)
        assert updated is not None
        assert updated.steps[0].status == GoalStepStatus.IN_PROGRESS
        assert "[→]" in store.to_prompt_string()

    def test_complete_step_then_goal(self, store: GoalStore) -> None:
        goal = store.create_goal(title="任务", steps=["A", "B"])
        for step in goal.steps:
            store.complete(goal.id, step_id=step.id)
        finished = store.get_goal(goal.id)
        assert finished is not None
        assert finished.status == GoalStatus.COMPLETED
        assert store.list_goals(include_completed=False) == []

    def test_checkpoint_roundtrip(self, store: GoalStore) -> None:
        goal = store.create_goal(title="持久化", steps=["步骤1"])
        data = store.to_checkpoint_data()
        store2 = GoalStore()
        store2.load_from_checkpoint(data)
        restored = store2.get_goal(goal.id)
        assert restored is not None
        assert restored.title == "持久化"
        assert len(restored.steps) == 1

    def test_has_active_goals(self, store: GoalStore) -> None:
        store.create_goal(title="任务", steps=["一步", "二步"])
        assert store.has_active_goals() is True

    def test_has_active_goals_when_blocked(self, store: GoalStore) -> None:
        goal = store.create_goal(title="任务", steps=["一步"])
        step_id = goal.steps[0].id
        store.update_goal(
            goal.id,
            step_id=step_id,
            step_status=GoalStepStatus.BLOCKED,
        )
        assert store.has_active_goals() is True
        assert store.goals_defer_auto_continue() is True
        assert store.should_auto_continue() is False

    def test_goals_defer_auto_continue_false_when_in_progress(
        self, store: GoalStore
    ) -> None:
        goal = store.create_goal(title="任务", steps=["等待", "下一步"])
        store.update_goal(
            goal.id,
            step_id=goal.steps[0].id,
            step_status=GoalStepStatus.IN_PROGRESS,
        )
        store.update_goal(
            goal.id,
            step_id=goal.steps[1].id,
            step_status=GoalStepStatus.BLOCKED,
        )
        assert store.goals_defer_auto_continue() is False

    def test_goals_defer_auto_continue_false_when_only_pending(
        self, store: GoalStore
    ) -> None:
        store.create_goal(title="任务", steps=["一步", "二步"])
        assert store.goals_defer_auto_continue() is False

    def test_no_active_goals_when_all_done(self, store: GoalStore) -> None:
        goal = store.create_goal(title="任务", steps=["一步"])
        store.complete(goal.id)
        assert store.has_active_goals() is False

    def test_build_goal_check_prompt(self, store: GoalStore) -> None:
        goal = store.create_goal(title="写报告", steps=["调研", "撰写"])
        prompt = store.build_goal_check_prompt()
        assert "[目标检查]" in prompt
        assert "goal_complete" in prompt
        assert goal.id in prompt
        assert "调研" in prompt


class TestGoalTools:
    @pytest.mark.asyncio
    async def test_goal_create(self, create_tool: GoalCreateTool) -> None:
        result = await create_tool.execute(
            title="竞品分析",
            steps=["搜索", "整理", "输出"],
        )
        assert result.success is True
        assert result.data["goal"]["title"] == "竞品分析"
        assert len(result.data["goal"]["steps"]) == 3

    @pytest.mark.asyncio
    async def test_goal_create_missing_title(
        self, create_tool: GoalCreateTool
    ) -> None:
        result = await create_tool.execute(title="  ")
        assert result.success is False
        assert result.error == "MISSING_TITLE"

    @pytest.mark.asyncio
    async def test_goal_update_and_complete(
        self,
        create_tool: GoalCreateTool,
        update_tool: GoalUpdateTool,
        complete_tool: GoalCompleteTool,
        list_tool: GoalListTool,
    ) -> None:
        created = await create_tool.execute(title="多步", steps=["一步", "二步"])
        goal_id = created.data["goal"]["id"]
        step_id = created.data["goal"]["steps"][0]["id"]

        in_progress = await update_tool.execute(
            goal_id=goal_id,
            step_id=step_id,
            status="in_progress",
        )
        assert in_progress.success is True

        done = await complete_tool.execute(goal_id=goal_id, step_id=step_id)
        assert done.success is True
        assert done.data["goal"]["steps"][0]["status"] == "completed"

        listed = await list_tool.execute()
        assert listed.success is True
        assert listed.data["count"] == 1

    @pytest.mark.asyncio
    async def test_goal_complete_entire_goal(
        self, create_tool: GoalCreateTool, complete_tool: GoalCompleteTool
    ) -> None:
        created = await create_tool.execute(title="整体完成", steps=["a"])
        goal_id = created.data["goal"]["id"]
        result = await complete_tool.execute(goal_id=goal_id)
        assert result.success is True
        assert result.data["goal"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_build_goal_tools(self, store: GoalStore) -> None:
        tools = build_goal_tools(store)
        names = {t.name for t in tools}
        assert names == {
            "goal_create",
            "goal_update",
            "goal_complete",
            "goal_list",
        }


class TestGoalCheckpoint:
    def test_checkpoint_manager_persists_goals(self, tmp_path: Path) -> None:
        ckpt_path = tmp_path / "checkpoint.json"
        mgr = CoreCheckpointManager(str(ckpt_path))
        goals = [
            {
                "id": "goal-test1234",
                "title": "测试",
                "description": None,
                "status": "active",
                "steps": [],
                "created_at": "2026-06-29T00:00:00+00:00",
                "updated_at": "2026-06-29T00:00:00+00:00",
            }
        ]
        mgr.write(
            CoreCheckpoint(
                session_id="sess-1",
                owner_id="root",
                source="cli",
                running_summary=None,
                recent_messages=[],
                last_active_at=1.0,
                remaining_ttl_seconds=1800.0,
                turn_count=1,
                last_history_id=0,
                token_usage={},
                active_goals=goals,
            )
        )
        loaded = mgr.read()
        assert loaded is not None
        assert loaded.active_goals == goals

    def test_legacy_checkpoint_without_goals(self, tmp_path: Path) -> None:
        ckpt_path = tmp_path / "legacy.json"
        ckpt_path.write_text(
            json.dumps(
                {
                    "session_id": "s",
                    "owner_id": "u",
                    "source": "cli",
                    "running_summary": None,
                    "recent_messages": [],
                    "last_active_at": 0.0,
                    "remaining_ttl_seconds": 1800.0,
                    "turn_count": 0,
                    "last_history_id": 0,
                    "token_usage": {},
                }
            ),
            encoding="utf-8",
        )
        loaded = CoreCheckpointManager(str(ckpt_path)).read()
        assert loaded is not None
        assert loaded.active_goals == []
