"""Tests for queue-driven Agent architecture.

Covers:
  - AgentTask model serialization / factory helpers
  - AgentTaskQueue push / pop_pending / update_status / list_recent / recover_stale_running
  - SessionManager ephemeral vs persistent behaviour (Agent mocked)
  - AutomationScheduler dispatches AgentTask to queue (not event_bus) when task_queue is set
"""

from __future__ import annotations

from datetime import datetime
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.interfaces import AgentRunResult
from system.automation.agent_task import (
    AgentTask,
    ContextPolicy,
    TaskStatus,
    make_cron_task,
    make_user_task,
)
from system.automation.task_queue import AgentTaskQueue


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def queue(tmp_path):
    return AgentTaskQueue(db_path=str(tmp_path / "agent_tasks.db"))


# ---------------------------------------------------------------------------
# AgentTask model
# ---------------------------------------------------------------------------


class TestAgentTaskModel:
    def test_defaults(self):
        task = AgentTask(
            source="cron:sync.course",
            session_id="cron:sync.course:2025-01-01",
            instruction="请同步课表",
        )
        assert task.status == TaskStatus.PENDING
        assert task.context_policy == ContextPolicy.EPHEMERAL
        assert task.result is None
        assert task.error is None
        assert task.task_id.startswith("task-")

    def test_serialization_round_trip(self):
        task = AgentTask(
            source="cli:root",
            session_id="cli:root",
            instruction="查看今天的日程",
            context_policy=ContextPolicy.PERSISTENT,
        )
        json_str = task.model_dump_json()
        restored = AgentTask.model_validate_json(json_str)
        assert restored.task_id == task.task_id
        assert restored.instruction == task.instruction
        assert restored.context_policy == ContextPolicy.PERSISTENT

    def test_make_cron_task(self):
        task = make_cron_task("sync.course", "请同步课表数据（Canvas）")
        assert task.source == "cron:sync.course"
        assert task.context_policy == ContextPolicy.EPHEMERAL
        assert task.session_id.startswith("cron:sync.course:")
        assert task.instruction == "请同步课表数据（Canvas）"
        assert task.user_id == "default"

    def test_make_user_task(self):
        task = make_user_task("帮我看今天待办", channel="social:wechat", user_id="u123")
        assert task.source == "social:wechat"
        assert task.context_policy == ContextPolicy.PERSISTENT
        assert task.session_id == "social:wechat:u123"

    def test_id_property(self):
        task = make_cron_task("summary.daily", "请生成今日摘要")
        assert task.id == task.task_id


# ---------------------------------------------------------------------------
# AgentTaskQueue
# ---------------------------------------------------------------------------


class TestAgentTaskQueue:
    def test_push_and_list(self, queue):
        task = make_cron_task("sync.course", "请同步课表")
        queue.push(task)

        items = queue.list_recent(limit=10)
        assert len(items) == 1
        assert items[0].task_id == task.task_id

    def test_pop_pending_returns_running_task(self, queue):
        task = make_cron_task("sync.email", "请同步邮件")
        queue.push(task)

        popped = queue.pop_pending()
        assert popped is not None
        assert popped.task_id == task.task_id
        assert popped.status == TaskStatus.RUNNING
        assert popped.started_at is not None

    def test_pop_pending_returns_none_when_empty(self, queue):
        assert queue.pop_pending() is None

    def test_pop_pending_fifo_order(self, queue):
        t1 = make_cron_task("sync.course", "任务1")
        t2 = make_cron_task("summary.daily", "任务2")
        queue.push(t1)
        queue.push(t2)

        first = queue.pop_pending()
        assert first is not None
        assert first.task_id == t1.task_id

    def test_update_status_success(self, queue):
        task = make_cron_task("summary.daily", "生成摘要")
        queue.push(task)
        popped = queue.pop_pending()
        assert popped is not None

        queue.update_status(popped.task_id, TaskStatus.SUCCESS, result="摘要已生成")

        items = queue.list_recent(status=TaskStatus.SUCCESS)
        assert len(items) == 1
        assert items[0].result == "摘要已生成"
        assert items[0].finished_at is not None

    def test_update_status_failed(self, queue):
        task = make_cron_task("sync.course", "同步课表")
        queue.push(task)
        popped = queue.pop_pending()
        assert popped is not None

        queue.update_status(popped.task_id, TaskStatus.FAILED, error="ConnectionError")

        items = queue.list_recent(status=TaskStatus.FAILED)
        assert len(items) == 1
        assert items[0].error == "ConnectionError"

    def test_recover_stale_running(self, queue):
        t1 = make_cron_task("sync.course", "任务1")
        t2 = make_cron_task("sync.email", "任务2")
        queue.push(t1)
        queue.push(t2)

        # 模拟两个任务在运行中被中断（未完成）
        queue.pop_pending()
        queue.pop_pending()

        running = queue.list_recent(status=TaskStatus.RUNNING)
        assert len(running) == 2

        recovered = queue.recover_stale_running()
        assert recovered == 2

        pending = queue.list_recent(status=TaskStatus.PENDING)
        assert len(pending) == 2

    def test_pending_count(self, queue):
        assert queue.pending_count() == 0
        queue.push(make_cron_task("sync.course", "任务1"))
        queue.push(make_cron_task("sync.email", "任务2"))
        assert queue.pending_count() == 2
        queue.pop_pending()
        assert queue.pending_count() == 1

    def test_running_count(self, queue):
        assert queue.running_count() == 0
        queue.push(make_cron_task("sync.course", "任务1"))
        assert queue.running_count() == 0
        queue.pop_pending()
        assert queue.running_count() == 1
        queue.update_status(
            queue.list_recent(status=TaskStatus.RUNNING)[0].task_id,
            TaskStatus.SUCCESS,
            result="ok",
        )
        assert queue.running_count() == 0

    def test_list_recent_filter_by_status(self, queue):
        t1 = make_cron_task("sync.course", "任务1")
        t2 = make_cron_task("sync.email", "任务2")
        queue.push(t1)
        queue.push(t2)
        queue.pop_pending()  # t1 -> running

        pending = queue.list_recent(status=TaskStatus.PENDING)
        running = queue.list_recent(status=TaskStatus.RUNNING)

        assert len(pending) == 1
        assert pending[0].task_id == t2.task_id
        assert len(running) == 1
        assert running[0].task_id == t1.task_id

    def test_push_resets_status_to_pending(self, queue):
        task = make_cron_task("sync.course", "任务")
        task.status = TaskStatus.FAILED
        queue.push(task)

        items = queue.list_recent(status=TaskStatus.PENDING)
        assert len(items) == 1


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


class TestSessionManager:
    """SessionManager 行为测试，Agent 完全 mock，不调用真实 LLM。"""

    def _make_mock_agent(self, response: str = "ok"):
        """创建 kernel 兼容的 mock agent，供 CoreSessionAdapter.run_turn 使用。"""
        from agent_core.kernel_interface import ReturnAction

        agent = AsyncMock()
        agent.prepare_turn = AsyncMock(return_value=1)
        agent._finalize_turn = AsyncMock()
        agent._tool_registry = MagicMock()
        agent._session_id = "mock"
        agent.close = AsyncMock()

        async def _run_loop(turn_id=0, hooks=None):
            yield ReturnAction(message=response, status="completed")

        agent.run_loop = _run_loop
        return agent

    @pytest.mark.asyncio
    async def test_ephemeral_creates_new_agent_each_time(self):
        from system.automation.session_manager import SessionManager

        def make_agent_factory():
            def factory():
                return []

            return factory

        manager = SessionManager(tools_factory=make_agent_factory())

        created_agents = []

        def fake_create_agent(session_id="", **kwargs):
            ag = self._make_mock_agent("done")
            created_agents.append(ag)
            return ag

        with patch.object(manager, "_create_agent", side_effect=fake_create_agent):
            await manager.run_task(
                "cron:sync.course:2025-01-01", "同步课表", ContextPolicy.EPHEMERAL
            )
            await manager.run_task(
                "cron:sync.course:2025-01-01", "同步课表", ContextPolicy.EPHEMERAL
            )

        # 两次调用应各创建一个独立 Agent
        assert len(created_agents) == 2
        # ephemeral agent 应在执行后关闭
        for ag in created_agents:
            ag.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_persistent_reuses_agent_for_same_session(self):
        from system.automation.session_manager import SessionManager

        manager = SessionManager(tools_factory=lambda: [])

        created_agents = []

        def fake_create_agent(session_id="", **kwargs):
            ag = self._make_mock_agent("pong")
            created_agents.append(ag)
            return ag

        with patch.object(manager, "_create_agent", side_effect=fake_create_agent):
            r1 = await manager.run_task("cli:root", "你好", ContextPolicy.PERSISTENT)
            r2 = await manager.run_task("cli:root", "再见", ContextPolicy.PERSISTENT)

        # 同一个 session_id 只创建一个 Agent
        assert len(created_agents) == 1
        # persistent agent 不主动关闭（由 close_all 统一清理）
        created_agents[0].close.assert_not_awaited()
        assert r1 == "pong"
        assert r2 == "pong"

    @pytest.mark.asyncio
    async def test_persistent_different_sessions_use_different_agents(self):
        from system.automation.session_manager import SessionManager

        manager = SessionManager(tools_factory=lambda: [])
        created_agents = []

        def fake_create_agent(session_id="", **kwargs):
            ag = self._make_mock_agent()
            created_agents.append(ag)
            return ag

        with patch.object(manager, "_create_agent", side_effect=fake_create_agent):
            await manager.run_task("cli:root", "来自 CLI", ContextPolicy.PERSISTENT)
            await manager.run_task(
                "social:wechat:u123", "来自微信", ContextPolicy.PERSISTENT
            )

        assert len(created_agents) == 2

    @pytest.mark.asyncio
    async def test_close_all_closes_persistent_sessions(self):
        from system.automation.session_manager import SessionManager

        manager = SessionManager(tools_factory=lambda: [])
        agents = []

        def fake_create_agent(session_id="", **kwargs):
            ag = self._make_mock_agent()
            agents.append(ag)
            return ag

        with patch.object(manager, "_create_agent", side_effect=fake_create_agent):
            await manager.run_task("cli:root", "ping", ContextPolicy.PERSISTENT)
            await manager.run_task("social:wechat:u1", "ping", ContextPolicy.PERSISTENT)
            await manager.close_all()

        for ag in agents:
            ag.close.assert_awaited_once()
        assert manager.active_sessions() == []

    @pytest.mark.asyncio
    async def test_run_task_uses_core_session_run_turn(self):
        from system.automation.session_manager import SessionManager

        manager = SessionManager(tools_factory=lambda: [])
        core_session = AsyncMock()
        core_session.run_turn = AsyncMock(return_value=AgentRunResult(output_text="ok"))
        core_session.activate_session = AsyncMock(return_value=None)
        core_session.close = AsyncMock()

        with patch.object(manager, "_create_session", return_value=core_session):
            result = await manager.run_task(
                session_id="cli:root",
                instruction="你好",
                context_policy=ContextPolicy.PERSISTENT,
            )

        assert result == "ok"
        core_session.activate_session.assert_awaited_once_with("cli:root")
        core_session.run_turn.assert_awaited_once()
        call_args = core_session.run_turn.await_args
        assert call_args.args[0].text == "你好"


# ---------------------------------------------------------------------------
# AutomationScheduler — queue dispatch
# ---------------------------------------------------------------------------


class TestSchedulerQueueDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_via_queue_pushes_agent_task(self, tmp_path):
        from system.automation.repositories import (
            JobDefinitionRepository,
            JobRunRepository,
        )
        from system.automation.scheduler import AutomationScheduler
        from system.automation.types import JobDefinition

        queue = AgentTaskQueue(db_path=str(tmp_path / "tasks.db"))
        job_def_repo = JobDefinitionRepository(base_dir=str(tmp_path / "automation"))
        job_run_repo = JobRunRepository(base_dir=str(tmp_path / "automation"))

        scheduler = AutomationScheduler(
            job_def_repo=job_def_repo,
            job_run_repo=job_run_repo,
            task_queue=queue,
        )

        job = JobDefinition(job_type="sync.course", interval_seconds=3600)
        await scheduler.run_job_once(job)

        tasks = queue.list_recent(limit=10)
        assert len(tasks) == 1
        assert tasks[0].source == "cron:sync.course"
        assert tasks[0].context_policy == ContextPolicy.EPHEMERAL
        assert "sync_canvas" in tasks[0].instruction

    @pytest.mark.asyncio
    async def test_dispatch_all_default_job_types(self, tmp_path):
        from system.automation.repositories import (
            JobDefinitionRepository,
            JobRunRepository,
        )
        from system.automation.scheduler import AutomationScheduler
        from system.automation.types import JobDefinition

        queue = AgentTaskQueue(db_path=str(tmp_path / "tasks.db"))
        job_def_repo = JobDefinitionRepository(base_dir=str(tmp_path / "automation"))
        job_run_repo = JobRunRepository(base_dir=str(tmp_path / "automation"))

        scheduler = AutomationScheduler(
            job_def_repo=job_def_repo,
            job_run_repo=job_run_repo,
            task_queue=queue,
        )

        job_types = ["sync.course", "sync.email", "summary.daily", "summary.weekly"]
        for jt in job_types:
            await scheduler.run_job_once(
                JobDefinition(job_type=jt, interval_seconds=3600)
            )

        tasks = queue.list_recent(limit=20)
        assert len(tasks) == len(job_types)
        sources = {t.source for t in tasks}
        assert sources == {f"cron:{jt}" for jt in job_types}

    @pytest.mark.asyncio
    async def test_fallback_to_event_bus_when_no_queue(self, tmp_path):
        """当未配置 task_queue 时，依然通过 event_bus 触发（旧路径兼容）。"""
        from system.automation.event_bus import AsyncEventBus
        from system.automation.repositories import (
            JobDefinitionRepository,
            JobRunRepository,
        )
        from system.automation.scheduler import AutomationScheduler
        from system.automation.types import JobDefinition

        bus = AsyncEventBus()
        received_topics: List[str] = []

        async def handler(event: dict):
            received_topics.append(event["topic"])

        bus.subscribe("sync.requested", handler)

        job_def_repo = JobDefinitionRepository(base_dir=str(tmp_path / "automation"))
        job_run_repo = JobRunRepository(base_dir=str(tmp_path / "automation"))

        scheduler = AutomationScheduler(
            event_bus=bus,
            job_def_repo=job_def_repo,
            job_run_repo=job_run_repo,
        )

        job = JobDefinition(job_type="sync.course", interval_seconds=3600)
        await scheduler.run_job_once(job)

        assert "sync.requested" in received_topics

    @pytest.mark.asyncio
    async def test_run_job_once_disables_one_shot_job(self, tmp_path):
        from datetime import timedelta
        from system.automation.repositories import (
            JobDefinitionRepository,
            JobRunRepository,
        )
        from system.automation.scheduler import AutomationScheduler
        from system.automation.types import JobDefinition

        queue = AgentTaskQueue(db_path=str(tmp_path / "tasks.db"))
        repo_dir = str(tmp_path / "automation")
        job_def_repo = JobDefinitionRepository(base_dir=repo_dir)
        job_run_repo = JobRunRepository(base_dir=repo_dir)

        scheduler = AutomationScheduler(
            job_def_repo=job_def_repo,
            job_run_repo=job_run_repo,
            task_queue=queue,
        )

        job = JobDefinition(
            job_type="sync.email",
            one_shot=True,
            run_at=datetime.now() + timedelta(seconds=1),
            interval_seconds=1,
        )
        job_def_repo.create(job)
        await scheduler.run_job_once(job)

        stored = job_def_repo.get(job.job_id)
        assert stored is not None
        assert stored.enabled is False
        assert stored.one_shot is True
