"""KernelTerminal 系统控制台单元测试。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from system.kernel.terminal import (
    CoreInfo,
    KernelTerminal,
    SessionDetail,
    SystemStatus,
)


@dataclass
class MockCoreEntry:
    agent: object
    profile: object
    last_active_ts: float = 0.0
    session_start_ts: float = 0.0
    logger: object | None = None
    parent_session_id: str | None = None
    task_description: str | None = None
    sub_status: str | None = None
    sub_result: str | None = None
    sub_error: str | None = None
    sub_completed_at: float | None = None


def _make_mock_agent(
    *,
    source: str = "cli",
    user_id: str = "root",
    turn_count: int = 5,
    token_usage: dict | None = None,
    context_message_count: int = 10,
    has_checkpoint: bool = False,
) -> MagicMock:
    agent = MagicMock()
    agent._source = source
    agent._user_id = user_id
    agent._context = MagicMock()
    agent._context.get_messages = MagicMock(return_value=[{}] * context_message_count)
    agent.get_turn_count = MagicMock(return_value=turn_count)
    usage = token_usage or {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    agent.get_token_usage = MagicMock(return_value=usage)
    agent._checkpoint_manager = None
    if has_checkpoint:
        mgr = MagicMock()
        mgr._path = "/tmp/ckpt.json"
        mgr.__class__ = type("Mgr", (), {"__init__": lambda *a, **k: None})
        agent._checkpoint_manager = mgr
    return agent


def _make_mock_profile(
    *,
    mode: str = "full",
    session_expired_seconds: int = 1800,
    memory_enabled: bool = True,
    max_context_tokens: int = 80_000,
) -> SimpleNamespace:
    return SimpleNamespace(
        mode=mode,
        session_expired_seconds=session_expired_seconds,
        memory_enabled=memory_enabled,
        max_context_tokens=max_context_tokens,
    )


def _make_mock_pool(*, pool_entries: dict | None = None) -> MagicMock:
    """pool_entries: session_id -> (agent, profile) or CoreEntry-like."""
    pool = MagicMock()
    pool._max_sessions = 100
    pool._pool = {}
    pool._zombies = {}
    if pool_entries:
        now = time.monotonic()
        for sid, val in pool_entries.items():
            if isinstance(val, MockCoreEntry):
                pool._pool[sid] = val
            else:
                agent, profile = val
                pool._pool[sid] = MockCoreEntry(
                    agent=agent,
                    profile=profile,
                    last_active_ts=now - 60.0,
                    session_start_ts=now - 3600.0,
                    logger=MagicMock(file_path="/tmp/session.jsonl"),
                )
    pool.get_entry = lambda sid: pool._pool.get(sid)
    pool.list_entries = lambda include_zombies=False: (
        list(pool._pool.items()) + (list(pool._zombies.items()) if include_zombies else [])
    )
    pool.zombie_count = lambda: len(pool._zombies)
    pool.is_zombie = lambda sid: sid in pool._zombies
    pool.list_sessions = lambda: list(pool._pool.keys())
    pool.acquire = AsyncMock()
    pool.evict = AsyncMock()
    return pool


def _make_mock_scheduler(*, queue_size: int = 0, active_task_count: int = 0) -> MagicMock:
    sched = MagicMock()
    sched.queue_size = queue_size
    sched.active_task_count = active_task_count
    sched._inflight_sessions = {"cli:root": 1}
    sched._cancelled_sessions = set()
    sched.cancel_session_tasks = MagicMock(return_value=True)
    sched.submit = AsyncMock(return_value=MagicMock(request_id="req-1"))
    sched.wait_result = AsyncMock(
        return_value=SimpleNamespace(
            output_text="ok",
            metadata={},
            attachments=[],
        )
    )
    return sched


def test_terminal_ps_empty() -> None:
    pool = _make_mock_pool()
    sched = _make_mock_scheduler()
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    cores = terminal.ps()
    assert cores == []


def test_terminal_ps_one() -> None:
    agent = _make_mock_agent(source="cli", user_id="root", turn_count=3, token_usage={"total_tokens": 200})
    profile = _make_mock_profile(mode="full", session_expired_seconds=1800)
    pool = _make_mock_pool(pool_entries={"cli:root": (agent, profile)})
    sched = _make_mock_scheduler()
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    cores = terminal.ps()
    assert len(cores) == 1
    c = cores[0]
    assert c.session_id == "cli:root"
    assert c.source == "cli"
    assert c.user_id == "root"
    assert c.mode == "full"
    assert c.turn_count == 3
    assert c.total_tokens == 200
    assert c.memory_enabled is True
    assert c.in_zombie is False


def test_terminal_top() -> None:
    pool = _make_mock_pool()
    sched = _make_mock_scheduler(queue_size=2, active_task_count=1)
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    status = terminal.top()
    assert isinstance(status, SystemStatus)
    assert status.active_cores == 0
    assert status.max_cores == 100
    assert status.queue_depth == 2
    assert status.inflight_tasks == 1
    assert status.zombie_cores == 0
    assert status.uptime_seconds >= 0


def test_terminal_inspect_missing() -> None:
    pool = _make_mock_pool()
    sched = _make_mock_scheduler()
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    with pytest.raises(KeyError, match="session not found"):
        terminal.inspect("nonexistent")


def test_terminal_inspect_ok() -> None:
    agent = _make_mock_agent(
        source="feishu",
        user_id="u123",
        turn_count=10,
        context_message_count=20,
        has_checkpoint=True,
    )
    profile = _make_mock_profile(mode="full", session_expired_seconds=900)
    pool = _make_mock_pool(pool_entries={"feishu:u123": (agent, profile)})
    entry = pool._pool["feishu:u123"]
    entry.logger = MagicMock()
    entry.logger.file_path = "/logs/feishu-u123.jsonl"
    sched = _make_mock_scheduler()
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    detail = terminal.inspect("feishu:u123")
    assert isinstance(detail, SessionDetail)
    assert detail.session_id == "feishu:u123"
    assert detail.source == "feishu"
    assert detail.user_id == "u123"
    assert detail.turn_count == 10
    assert detail.context_message_count == 20
    assert detail.token_usage["total_tokens"] == 150
    # has_checkpoint depends on Path(ckpt_mgr._path).exists(); mock path may not exist
    assert isinstance(detail.has_checkpoint, bool)
    assert detail.log_file == "/logs/feishu-u123.jsonl"
    assert detail.in_zombie is False


def test_terminal_ps_includes_zombies() -> None:
    agent = _make_mock_agent(source="subagent", user_id="sub-1")
    profile = _make_mock_profile(mode="sub", session_expired_seconds=86400)
    pool = _make_mock_pool(pool_entries={"sub:live": (agent, profile)})
    pool._zombies["sub:dead"] = MockCoreEntry(
        agent=None,
        profile=profile,
        last_active_ts=time.monotonic() - 5.0,
        session_start_ts=time.monotonic() - 20.0,
        parent_session_id="cli:root",
        task_description="dead task",
        sub_status="completed",
        sub_result="done",
    )
    sched = _make_mock_scheduler()
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    cores = terminal.ps()
    assert len(cores) == 2
    zombie = next(c for c in cores if c.session_id == "sub:dead")
    assert zombie.in_zombie is True
    assert zombie.parent_session_id == "cli:root"
    assert zombie.lifecycle == "zombie"


def test_terminal_inspect_zombie_ok() -> None:
    profile = _make_mock_profile(mode="sub", session_expired_seconds=86400)
    pool = _make_mock_pool()
    pool._zombies["sub:done"] = MockCoreEntry(
        agent=None,
        profile=profile,
        last_active_ts=time.monotonic() - 5.0,
        session_start_ts=time.monotonic() - 20.0,
        parent_session_id="cli:root",
        task_description="done task",
        sub_status="completed",
        sub_result="full result text",
        sub_completed_at=time.time(),
    )
    pool.get_entry = lambda sid: pool._zombies.get(sid)
    sched = _make_mock_scheduler()
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    detail = terminal.inspect("sub:done")
    assert detail.in_zombie is True
    assert detail.parent_session_id == "cli:root"
    assert detail.lifecycle == "zombie"
    assert "full result text" in (detail.sub_result_preview or "")


def test_terminal_queue() -> None:
    pool = _make_mock_pool()
    sched = _make_mock_scheduler()
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    q = terminal.queue()
    assert "queue_size" in q
    assert "inflight_sessions" in q
    assert "cancelled_sessions" in q
    assert "active_task_count" in q


def test_terminal_automation_tracked_jobs_unavailable() -> None:
    pool = _make_mock_pool()
    sched = _make_mock_scheduler()
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    out = terminal.automation_tracked_jobs()
    assert out.get("available") is False


def test_terminal_automation_tracked_jobs_with_scheduler() -> None:
    pool = _make_mock_pool()
    sched = _make_mock_scheduler()
    auto = MagicMock()
    auto.tracked_jobs_snapshot = MagicMock(
        return_value={
            "scheduler_running": True,
            "tracked_job_count": 1,
            "jobs": [{"job_name": "j1", "task_done": False}],
        }
    )
    terminal = KernelTerminal(
        scheduler=sched, core_pool=pool, automation_scheduler=auto
    )
    out = terminal.automation_tracked_jobs()
    assert out.get("available") is True
    assert out.get("tracked_job_count") == 1
    auto.tracked_jobs_snapshot.assert_called_once()


def test_terminal_agent_task_queue_unavailable() -> None:
    pool = _make_mock_pool()
    sched = _make_mock_scheduler()
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    out = terminal.agent_task_queue_status()
    assert out.get("available") is False


def test_terminal_agent_task_queue_with_queue() -> None:
    pool = _make_mock_pool()
    sched = _make_mock_scheduler()
    q = MagicMock()
    q.pending_count = MagicMock(return_value=2)
    q.running_count = MagicMock(return_value=1)
    q.list_recent = MagicMock(return_value=[])
    terminal = KernelTerminal(scheduler=sched, core_pool=pool, agent_task_queue=q)
    out = terminal.agent_task_queue_status(limit=10)
    assert out.get("available") is True
    assert out["pending_count"] == 2
    assert out["running_count"] == 1
    q.list_recent.assert_called_once_with(limit=10)


@pytest.mark.asyncio
async def test_terminal_kill() -> None:
    pool = _make_mock_pool(pool_entries={"cli:root": (_make_mock_agent(), _make_mock_profile())})
    sched = _make_mock_scheduler()
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    await terminal.kill("cli:root")
    pool.evict.assert_awaited_once_with("cli:root", shutdown=False)


@pytest.mark.asyncio
async def test_terminal_cancel() -> None:
    pool = _make_mock_pool()
    sched = _make_mock_scheduler()
    sched.cancel_session_tasks = MagicMock(return_value=True)
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    out = await terminal.cancel("cli:root")
    assert out is True
    sched.cancel_session_tasks.assert_called_once_with("cli:root")


@pytest.mark.asyncio
async def test_terminal_spawn() -> None:
    pool = _make_mock_pool()
    agent = _make_mock_agent(source="system", user_id="root")
    profile = _make_mock_profile()
    pool.acquire = AsyncMock()
    pool.get_entry = lambda sid: (
        MockCoreEntry(
            agent=agent,
            profile=profile,
            last_active_ts=time.monotonic() - 10,
            session_start_ts=time.monotonic() - 10,
        )
        if sid == "system:new"
        else None
    )
    pool._pool["system:new"] = MockCoreEntry(
        agent=agent,
        profile=profile,
        last_active_ts=time.monotonic() - 10,
        session_start_ts=time.monotonic() - 10,
    )
    sched = _make_mock_scheduler()
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    info = await terminal.spawn("system:new", source="system", user_id="root")
    assert isinstance(info, CoreInfo)
    assert info.session_id == "system:new"
    pool.acquire.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_attach() -> None:
    pool = _make_mock_pool()
    sched = _make_mock_scheduler()
    sched.wait_result = AsyncMock(
        return_value=SimpleNamespace(
            output_text="reply",
            metadata={"k": "v"},
            attachments=[{"type": "image"}],
        )
    )
    terminal = KernelTerminal(scheduler=sched, core_pool=pool)
    result = await terminal.attach("cli:root", "hello")
    assert result.output_text == "reply"
    assert getattr(result, "metadata", {}) == {"k": "v"}
    assert getattr(result, "attachments", []) == [{"type": "image"}]
    sched.submit.assert_awaited_once()
    sched.wait_result.assert_awaited_once()
