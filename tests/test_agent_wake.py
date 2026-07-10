from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest

from agent_core.kernel_interface.action import KernelRequest
from agent_core.tools.agent_wake import (
    cancel_pending_wakes,
    cancel_wake,
    clear_all_wakes_for_tests,
    deliver_wake_via_inject,
    flush_pending_wakes_for_session,
    format_wake_notification,
    list_wakes,
    poll_due_wakes,
    register_wake,
    session_has_deferred_agent_wake,
)
from agent_core.tools.bash_job_notify import set_notify_dependencies

pytestmark = pytest.mark.asyncio


class _FakeScheduler:
    def __init__(self) -> None:
        self.inflight: Dict[str, int] = {}
        self.injected: List[KernelRequest] = []

    def session_inflight_request_count(self, session_id: str) -> int:
        return self.inflight.get(session_id, 0)

    def inject_turn(self, request: KernelRequest) -> None:
        self.injected.append(request)


def _wake(
    session_id: str = "cli:root",
    wake_id: str = "wake-test-1",
    message: str = "该继续了",
    fire_at: float | None = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    return {
        "wake_id": wake_id,
        "session_id": session_id,
        "message": message,
        "label": kwargs.get("label", ""),
        "source": "cli",
        "user_id": "root",
        "fire_at": fire_at if fire_at is not None else time.time() - 1,
    }


async def test_register_and_poll_due_wake():
    clear_all_wakes_for_tests()
    fire_at = time.time() + 60
    wid = register_wake(
        session_id="cli:root",
        fire_at=fire_at,
        message="hello",
        label="test",
    )
    assert wid
    assert len(poll_due_wakes()) == 0

    wid2 = register_wake(
        session_id="cli:root",
        fire_at=time.time() - 1,
        message="due now",
    )
    due = poll_due_wakes()
    assert len(due) == 1
    assert due[0]["wake_id"] == wid2


async def test_format_wake_notification():
    text = format_wake_notification(
        _wake(label="午休", message="起来干活", wake_id="w1")
    )
    assert "[定时唤醒] 午休" in text
    assert "起来干活" in text
    assert "wake_id=w1" in text


async def test_deliver_inject_when_no_inflight():
    clear_all_wakes_for_tests()
    scheduler = _FakeScheduler()
    set_notify_dependencies(scheduler=scheduler, core_pool=None)

    register_wake(
        session_id="feishu:user:ou_test:1",
        fire_at=time.time() - 1,
        message="继续写代码",
        metadata={"feishu_chat_id": "oc_wake_chat"},
    )
    wake = poll_due_wakes()[0]
    ok = deliver_wake_via_inject(wake=wake)
    assert ok is True
    assert len(scheduler.injected) == 1
    req = scheduler.injected[0]
    assert req.session_id == "feishu:user:ou_test:1"
    assert req.frontend_id == "agent_wake"
    assert "继续写代码" in req.text
    assert (req.metadata or {}).get("feishu_chat_id") == "oc_wake_chat"
    assert (req.metadata or {}).get("_hooks") is not None
    assert (req.metadata or {}).get("_wake_id") == wake["wake_id"]
    items = list_wakes()
    assert len(items) == 1
    assert items[0]["staged"] is True


async def test_deliver_inject_when_no_inflight_cli():
    clear_all_wakes_for_tests()
    scheduler = _FakeScheduler()
    set_notify_dependencies(scheduler=scheduler, core_pool=None)

    register_wake(
        session_id="cli:root",
        fire_at=time.time() - 1,
        message="继续写代码",
    )
    wake = poll_due_wakes()[0]
    ok = deliver_wake_via_inject(wake=wake)
    assert ok is True
    assert len(scheduler.injected) == 1
    req = scheduler.injected[0]
    assert req.session_id == "cli:root"
    assert req.frontend_id == "agent_wake"
    assert "继续写代码" in req.text
    items = list_wakes()
    assert len(items) == 1
    assert items[0]["staged"] is True


async def test_deliver_stages_when_inflight():
    clear_all_wakes_for_tests()
    scheduler = _FakeScheduler()
    scheduler.inflight["cli:root"] = 1
    set_notify_dependencies(scheduler=scheduler, core_pool=None)

    register_wake(
        session_id="cli:root",
        fire_at=time.time() - 1,
        message="staged",
        wake_id="wake-stage-1",
    )
    wake = poll_due_wakes()[0]
    ok = deliver_wake_via_inject(wake=wake)
    assert ok is True
    assert len(scheduler.injected) == 0
    assert poll_due_wakes() == []

    scheduler.inflight["cli:root"] = 0
    flush_pending_wakes_for_session("cli:root")
    assert len(scheduler.injected) == 1
    assert "staged" in scheduler.injected[0].text
    items = list_wakes()
    assert len(items) == 1
    assert items[0]["staged"] is True


async def test_wake_staged_until_kernel_confirms_delivery():
    """inject 后仅 staged；abort 可重试，confirm 后才从注册表移除。"""
    from agent_core.tools.agent_wake import abort_wake_delivery, confirm_wake_delivered

    clear_all_wakes_for_tests()
    scheduler = _FakeScheduler()
    set_notify_dependencies(scheduler=scheduler, core_pool=None)

    wid = register_wake(
        session_id="cli:root",
        fire_at=time.time() - 1,
        message="retry me",
        wake_id="wake-retry-1",
    )
    wake = poll_due_wakes()[0]
    ok = deliver_wake_via_inject(wake=wake, scheduler=scheduler)
    assert ok is True
    assert len(list_wakes()) == 1
    assert list_wakes()[0]["staged"] is True

    abort_wake_delivery(wid)
    due = poll_due_wakes()
    assert len(due) == 1
    assert due[0]["wake_id"] == wid

    confirm_wake_delivered(wid)
    assert list_wakes() == []


async def test_cancel_wake():
    clear_all_wakes_for_tests()
    wid = register_wake(
        session_id="cli:root",
        fire_at=time.time() + 3600,
        message="later",
    )
    assert cancel_wake(wid) is True
    assert cancel_wake(wid) is False
    assert list_wakes() == []


async def test_schedule_wake_tool_relative():
    from system.tools.automation_tools import ScheduleWakeTool

    clear_all_wakes_for_tests()
    tool = ScheduleWakeTool()
    result = await tool.execute(
        message="5 分钟后提醒",
        delay_seconds=300,
        label="提醒",
        __execution_context__={
            "session_id": "cli:root",
            "source": "cli",
            "user_id": "root",
        },
    )
    assert result.success is True
    assert result.data["wake_id"]
    items = list_wakes(session_id="cli:root")
    assert len(items) == 1
    assert 250 <= items[0]["seconds_until"] <= 310


async def test_feishu_push_watch_file(tmp_path, monkeypatch):
    from system.automation.repositories import _automation_base_dir

    monkeypatch.setenv("SCHEDULE_AGENT_TEST_DATA_DIR", str(tmp_path))
    clear_all_wakes_for_tests()
    register_wake(
        session_id="feishu:user:ou_x:99",
        fire_at=time.time() + 3600,
        message="later",
        metadata={"feishu_chat_id": "oc_abc"},
    )
    from agent_core.tools.agent_wake import (
        load_feishu_push_watch_targets_from_file,
        list_feishu_push_watch_targets,
    )

    targets = list_feishu_push_watch_targets()
    assert len(targets) == 1
    assert targets[0]["chat_id"] == "oc_abc"
    loaded = load_feishu_push_watch_targets_from_file()
    assert len(loaded) == 1
    assert loaded[0]["session_id"] == "feishu:user:ou_x:99"
    assert (_automation_base_dir() / "feishu_wake_push_watch.json").is_file()


async def test_session_has_deferred_agent_wake_ignores_goal_check() -> None:
    clear_all_wakes_for_tests()
    sid = "feishu:user:ou_test:1"
    register_wake(
        session_id=sid,
        fire_at=time.time() + 1.0,
        message="goal check",
        label="goal-check",
    )
    assert session_has_deferred_agent_wake(sid) is False

    register_wake(
        session_id=sid,
        fire_at=time.time() + 900,
        message="check training",
        label="v5-monitor",
    )
    assert session_has_deferred_agent_wake(sid) is True


async def test_schedule_wake_cancels_goal_check_wakes() -> None:
    from system.tools.automation_tools import ScheduleWakeTool

    clear_all_wakes_for_tests()
    sid = "cli:root"
    register_wake(
        session_id=sid,
        fire_at=time.time() + 60,
        message="system nudge",
        label="goal-check",
    )
    assert len(list_wakes(session_id=sid)) == 1

    tool = ScheduleWakeTool()
    result = await tool.execute(
        message="15 分钟后检查训练",
        delay_minutes=15,
        label="v5-monitor",
        __execution_context__={
            "session_id": sid,
            "source": "cli",
            "user_id": "root",
        },
    )
    assert result.success is True
    items = list_wakes(session_id=sid)
    assert len(items) == 1
    assert items[0]["label"] == "v5-monitor"


async def test_cancel_pending_wakes_by_label() -> None:
    clear_all_wakes_for_tests()
    sid = "cli:root"
    register_wake(
        session_id=sid,
        fire_at=time.time() + 60,
        message="a",
        label="goal-check",
    )
    register_wake(
        session_id=sid,
        fire_at=time.time() + 120,
        message="b",
        label="other",
    )
    assert cancel_pending_wakes(sid, label="goal-check") == 1
    items = list_wakes(session_id=sid)
    assert len(items) == 1
    assert items[0]["label"] == "other"


    from system.tools.automation_tools import ScheduleWakeTool

    tool = ScheduleWakeTool()
    result = await tool.execute(
        message="hi",
        __execution_context__={"session_id": "cli:root"},
    )
    assert result.success is False
    assert result.error == "INVALID_TIME"


class _FakeGoalAgent:
    def __init__(self, store) -> None:
        self._goal_store = store


class _FakeCorePoolEntry:
    def __init__(self, agent) -> None:
        self.agent = agent


class _FakeCorePool:
    def __init__(self, agent) -> None:
        self._entry = _FakeCorePoolEntry(agent)

    def get_entry(self, session_id: str):
        return self._entry


async def test_cancel_staged_wake_not_flushed() -> None:
    """取消已暂存的 wake 后 flush 不应再投递。"""
    clear_all_wakes_for_tests()
    scheduler = _FakeScheduler()
    scheduler.inflight["cli:root"] = 1
    set_notify_dependencies(scheduler=scheduler, core_pool=None)

    register_wake(
        session_id="cli:root",
        fire_at=time.time() - 1,
        message="goal nudge",
        label="goal-check",
        wake_id="wake-staged-cancel",
    )
    wake = poll_due_wakes()[0]
    assert deliver_wake_via_inject(wake=wake) is True
    assert len(scheduler.injected) == 0

    cancel_pending_wakes("cli:root", label="goal-check")
    scheduler.inflight["cli:root"] = 0
    flush_pending_wakes_for_session("cli:root")
    assert len(scheduler.injected) == 0


async def test_stale_goal_check_wake_skipped_when_no_active_goals() -> None:
    from agent_core.goals.store import GoalStore

    clear_all_wakes_for_tests()
    pool = _FakeCorePool(_FakeGoalAgent(GoalStore()))
    scheduler = _FakeScheduler()
    set_notify_dependencies(scheduler=scheduler, core_pool=pool)

    register_wake(
        session_id="cli:root",
        fire_at=time.time() - 1,
        message="check",
        label="goal-check",
    )
    wake = poll_due_wakes()[0]
    ok = deliver_wake_via_inject(wake=wake)
    assert ok is False
    assert len(scheduler.injected) == 0
    assert list_wakes() == []


async def test_stale_goal_check_wake_skipped_when_deferred() -> None:
    from agent_core.goals.store import GoalStore
    from agent_core.goals.types import GoalStepStatus

    clear_all_wakes_for_tests()
    store = GoalStore()
    goal = store.create_goal(title="训练监控", steps=["等待 GPU"])
    store.update_goal(
        goal.id,
        step_id=goal.steps[0].id,
        step_status=GoalStepStatus.BLOCKED,
        step_notes="缺 API key",
    )
    pool = _FakeCorePool(_FakeGoalAgent(store))
    scheduler = _FakeScheduler()
    set_notify_dependencies(scheduler=scheduler, core_pool=pool)

    register_wake(
        session_id="cli:root",
        fire_at=time.time() - 1,
        message="check",
        label="goal-check",
    )
    wake = poll_due_wakes()[0]
    ok = deliver_wake_via_inject(wake=wake)
    assert ok is False
    assert len(scheduler.injected) == 0
    assert list_wakes() == []
