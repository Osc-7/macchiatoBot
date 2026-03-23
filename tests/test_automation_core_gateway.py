"""AutomationCoreGateway tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from system.automation import AutomationCoreGateway, SessionCutPolicy, SessionRegistry
from system.kernel import CorePool
from agent_core.interfaces import (
    AgentHooks,
    AgentRunInput,
    AgentRunResult,
    InjectMessageCommand,
)


def _make_mock_scheduler(
    *,
    default_result: AgentRunResult | None = None,
    results_by_session: dict[str, AgentRunResult] | None = None,
    pool_sessions: list[str] | None = None,
    pool_entries: dict[str, MagicMock] | None = None,
):
    """创建 mock KernelScheduler，用于 Gateway 测试。"""
    results_by_session = results_by_session or {}
    default_result = default_result or AgentRunResult(output_text="ok")
    pool_sessions = pool_sessions or []
    pool_entries = pool_entries or {}

    async def _submit(request):
        sid = getattr(request, "session_id", "cli:root")

        class Handle:
            request_id = getattr(request, "request_id", "mock-req-id")
            session_id = sid

            def __await__(self):
                return _wait_result(self).__await__()

        return Handle()

    async def _wait_result(handle):
        sid = getattr(handle, "session_id", "cli:root")
        return results_by_session.get(sid, default_result)

    mock_pool = MagicMock(spec=CorePool)
    mock_pool.list_sessions = MagicMock(return_value=pool_sessions)
    mock_pool.has_session = MagicMock(side_effect=lambda s: s in pool_sessions)
    mock_pool.evict = AsyncMock()
    mock_pool.get_entry = MagicMock(
        side_effect=lambda sid: pool_entries.get(sid)
    )

    mock_scheduler = MagicMock()
    mock_scheduler.submit = AsyncMock(side_effect=_submit)
    mock_scheduler.wait_result = AsyncMock(side_effect=_wait_result)
    mock_scheduler.core_pool = mock_pool
    mock_scheduler.subscribe_out = MagicMock(return_value="mock-sub-id")
    mock_scheduler.unsubscribe_out = MagicMock()

    return mock_scheduler


def _make_gateway(
    tmp_path,
    core=None,
    *,
    kernel_scheduler=None,
    session_id: str = "cli:root",
    session_registry=None,
    session_factory=None,
    **kwargs,
):
    core = core or AsyncMock()
    if not hasattr(core, "run_turn"):
        core.run_turn = AsyncMock(return_value=AgentRunResult(output_text="ok"))
    if not hasattr(core, "get_session_state"):
        core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    if kernel_scheduler is None:
        kernel_scheduler = _make_mock_scheduler(
            default_result=AgentRunResult(output_text="ok")
        )
    registry = session_registry or SessionRegistry(str(tmp_path / "sessions.db"))
    return AutomationCoreGateway(
        core,
        kernel_scheduler=kernel_scheduler,
        session_id=session_id,
        session_registry=registry,
        session_factory=session_factory,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_gateway_dispatches_run_turn_via_scheduler(tmp_path):
    result_ok = AgentRunResult(output_text="ok")
    scheduler = _make_mock_scheduler(default_result=result_ok)
    core = AsyncMock()
    core.get_session_state = MagicMock(return_value=MagicMock(turn_count=1))

    gateway = _make_gateway(tmp_path, core, kernel_scheduler=scheduler)
    result = await gateway.run_turn(AgentRunInput(text="hello"), hooks=AgentHooks())

    assert result.output_text == "ok"
    scheduler.submit.assert_awaited_once()
    scheduler.wait_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_expire_flow_calls_evict(tmp_path):
    scheduler = _make_mock_scheduler()
    core = AsyncMock()
    core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    registry = SessionRegistry(str(tmp_path / "sessions.db"))
    gateway = _make_gateway(
        tmp_path,
        core,
        kernel_scheduler=scheduler,
        policy=SessionCutPolicy(idle_timeout_minutes=0, daily_cutoff_hour=4),
        session_registry=registry,
    )
    changed = await gateway.expire_session_if_needed(reason="idle_timeout")

    assert changed is True
    scheduler.core_pool.evict.assert_awaited_once_with("cli:root")
    assert registry.is_expired("root", "cli", "cli:root") is True


@pytest.mark.asyncio
async def test_gateway_expire_session_calls_evict(tmp_path):
    scheduler = _make_mock_scheduler()
    core = AsyncMock()
    core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    gateway = _make_gateway(tmp_path, core, kernel_scheduler=scheduler)
    await gateway.expire_session(reason="manual")

    scheduler.core_pool.evict.assert_awaited_once_with("cli:root")


@pytest.mark.asyncio
async def test_gateway_inject_message_submits_to_scheduler(tmp_path):
    result_ok = AgentRunResult(output_text="ok")
    scheduler = _make_mock_scheduler(default_result=result_ok)
    core = AsyncMock()
    core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    gateway = _make_gateway(tmp_path, core, kernel_scheduler=scheduler)
    result = await gateway.inject_message(
        command=InjectMessageCommand(
            session_id="wechat:user-1",
            input=AgentRunInput(
                text="hello",
                metadata={
                    "from_input": "1",
                    "trace_id": "input",
                    "session_id": "bad-input",
                },
            ),
            metadata={
                "from_command": "2",
                "trace_id": "command",
                "session_id": "bad-command",
            },
        ),
        hooks=AgentHooks(),
    )

    assert result.output_text == "ok"
    call = scheduler.submit.await_args
    req = call[0][0]
    assert req.session_id == "wechat:user-1"
    assert req.text == "hello"


@pytest.mark.asyncio
async def test_gateway_switch_session_upserts_and_routes(tmp_path):
    scheduler = _make_mock_scheduler(
        results_by_session={
            "cli:root": AgentRunResult(output_text="default"),
            "cli:work": AgentRunResult(output_text="new"),
        }
    )
    core = AsyncMock()
    core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    gateway = _make_gateway(tmp_path, core, kernel_scheduler=scheduler)
    created = await gateway.switch_session("cli:work")
    result = await gateway.run_turn(AgentRunInput(text="hello"), hooks=AgentHooks())

    assert created is True
    assert result.output_text == "new"
    assert "cli:root" in gateway.list_sessions()
    assert "cli:work" in gateway.list_sessions()


@pytest.mark.asyncio
async def test_gateway_inject_message_uses_target_session(tmp_path):
    scheduler = _make_mock_scheduler(
        results_by_session={
            "cli:root": AgentRunResult(output_text="default"),
            "wx:u1": AgentRunResult(output_text="other"),
        }
    )
    core = AsyncMock()
    core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    gateway = _make_gateway(tmp_path, core, kernel_scheduler=scheduler)
    inject_result = await gateway.inject_message(
        InjectMessageCommand(session_id="wx:u1", input=AgentRunInput(text="push")),
        hooks=AgentHooks(),
    )
    result = await gateway.run_turn(AgentRunInput(text="local"), hooks=AgentHooks())

    assert gateway.active_session_id == "cli:root"
    assert inject_result.output_text == "other"
    assert result.output_text == "default"
    assert scheduler.submit.await_count == 2


@pytest.mark.asyncio
async def test_gateway_close_does_not_close_initial_session(tmp_path):
    """Gateway 不拥有构造函数传入的初始 session，close 时不关闭它。"""
    scheduler = _make_mock_scheduler()
    core_default = AsyncMock()
    core_default.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    core_default.close = AsyncMock()

    gateway = _make_gateway(
        tmp_path,
        core_default,
        kernel_scheduler=scheduler,
    )
    await gateway.close()

    core_default.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_sessions_visible_across_instances(tmp_path):
    """SessionRegistry 跨实例共享，gw_a 创建的会话对 gw_b 可见。"""
    db_path = str(tmp_path / "sessions.db")
    scheduler_a = _make_mock_scheduler()
    core_a = AsyncMock()
    core_a.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    gw_a = _make_gateway(
        tmp_path,
        core_a,
        kernel_scheduler=scheduler_a,
        session_registry=SessionRegistry(db_path),
        owner_id="root",
        source="cli",
    )
    await gw_a.switch_session("cli:work")
    await gw_a.close()

    scheduler_b = _make_mock_scheduler()
    core_b = AsyncMock()
    core_b.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    gw_b = _make_gateway(
        tmp_path,
        core_b,
        kernel_scheduler=scheduler_b,
        session_registry=SessionRegistry(db_path),
        owner_id="root",
        source="cli",
    )
    sessions = gw_b.list_sessions()
    await gw_b.close()

    assert "cli:root" in sessions
    assert "cli:work" in sessions


@pytest.mark.asyncio
async def test_gateway_should_expire_uses_registry_timestamp_for_unloaded_session(
    tmp_path,
):
    db_path = str(tmp_path / "sessions.db")
    registry = SessionRegistry(db_path)
    registry.upsert_session("root", "cli", "cli:stale")
    stale_ts = (datetime.utcnow() - timedelta(minutes=120)).isoformat()
    registry._conn.execute(  # type: ignore[attr-defined]
        "UPDATE sessions SET updated_at=? WHERE owner_id=? AND source=? AND session_id=?",
        (stale_ts, "root", "cli", "cli:stale"),
    )
    registry._conn.commit()  # type: ignore[attr-defined]

    scheduler = _make_mock_scheduler()
    core = AsyncMock()
    core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    gateway = _make_gateway(
        tmp_path,
        core,
        kernel_scheduler=scheduler,
        session_id="cli:root",
        policy=SessionCutPolicy(idle_timeout_minutes=30, daily_cutoff_hour=4),
        session_registry=registry,
        owner_id="root",
        source="cli",
    )
    assert gateway.should_expire_session("cli:stale") is True
    await gateway.close()


@pytest.mark.asyncio
async def test_gateway_expired_session_not_repeated_until_activity(tmp_path):
    registry = SessionRegistry(str(tmp_path / "sessions.db"))
    scheduler = _make_mock_scheduler()
    core = AsyncMock()
    core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    gateway = _make_gateway(
        tmp_path,
        core,
        kernel_scheduler=scheduler,
        policy=SessionCutPolicy(idle_timeout_minutes=0, daily_cutoff_hour=4),
        session_registry=registry,
    )
    changed_1 = await gateway.expire_session_if_needed(reason="idle")
    changed_2 = await gateway.expire_session_if_needed(reason="idle")
    assert changed_1 is True
    assert changed_2 is False

    gateway.mark_activity("cli:root")
    changed_3 = await gateway.expire_session_if_needed(reason="idle")
    assert changed_3 is True
    await gateway.close()


@pytest.mark.asyncio
async def test_gateway_expire_unloaded_session_marks_only(tmp_path):
    scheduler = _make_mock_scheduler()
    core_default = AsyncMock()
    core_default.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    registry = SessionRegistry(str(tmp_path / "sessions.db"))
    registry.upsert_session("root", "cli", "cli:cold")

    gateway = _make_gateway(
        tmp_path,
        core_default,
        kernel_scheduler=scheduler,
        session_registry=registry,
        owner_id="root",
        source="cli",
    )
    await gateway.expire_session(reason="timer", session_id="cli:cold")

    scheduler.core_pool.evict.assert_awaited_once_with("cli:cold")
    assert registry.is_expired("root", "cli", "cli:cold") is True
    await gateway.close()


@pytest.mark.asyncio
async def test_gateway_switch_session_to_existing_routes_to_scheduler(tmp_path):
    """切换至已存在会话时，run_turn 经 scheduler 路由到对应 session。"""
    scheduler = _make_mock_scheduler(
        results_by_session={
            "cli:root": AgentRunResult(output_text="default"),
            "cli:expired": AgentRunResult(output_text="new"),
        }
    )
    core_default = AsyncMock()
    core_default.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    registry = SessionRegistry(str(tmp_path / "sessions.db"))
    registry.upsert_session("root", "cli", "cli:expired")
    registry.mark_expired("root", "cli", "cli:expired")

    gateway = _make_gateway(
        tmp_path,
        core_default,
        kernel_scheduler=scheduler,
        session_registry=registry,
    )
    await gateway.switch_session("cli:expired", create_if_missing=False)
    result = await gateway.run_turn(AgentRunInput(text="hi"), hooks=AgentHooks())

    assert result.output_text == "new"
    await gateway.close()


@pytest.mark.asyncio
async def test_gateway_delete_session_returns_false_when_history_delete_fails(tmp_path):
    registry = SessionRegistry(str(tmp_path / "sessions.db"))
    scheduler = _make_mock_scheduler()
    core_default = AsyncMock()
    core_default.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    broken_core = AsyncMock()
    broken_core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    broken_core.activate_session = AsyncMock(return_value=None)
    broken_core.delete_session_history = MagicMock(
        side_effect=RuntimeError("db write failed")
    )
    broken_core.close = AsyncMock()
    factory = AsyncMock(return_value=broken_core)

    gateway = _make_gateway(
        tmp_path,
        core_default,
        kernel_scheduler=scheduler,
        session_factory=factory,
        session_registry=registry,
    )
    await gateway.ensure_session("cli:work")

    ok = await gateway.delete_session("cli:work")
    sessions = gateway.list_sessions()
    assert ok is False
    assert registry.session_exists("root", "cli", "cli:work") is True
    assert "cli:work" in sessions
    await gateway.close()


@pytest.mark.asyncio
async def test_gateway_delete_session_returns_false_without_core_session_for_cold_session(
    tmp_path,
):
    registry = SessionRegistry(str(tmp_path / "sessions.db"))
    registry.upsert_session("root", "cli", "cli:cold")

    scheduler = _make_mock_scheduler()
    core_default = AsyncMock()
    core_default.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    gateway = _make_gateway(
        tmp_path,
        core_default,
        kernel_scheduler=scheduler,
        session_registry=registry,
        owner_id="root",
        source="cli",
    )

    ok = await gateway.delete_session("cli:cold")
    assert ok is False
    assert registry.session_exists("root", "cli", "cli:cold") is True
    await gateway.close()
