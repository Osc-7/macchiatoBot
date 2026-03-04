"""AutomationCoreGateway tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_agent.automation import AutomationCoreGateway, SessionCutPolicy, SessionRegistry
from schedule_agent.core.interfaces import AgentHooks, AgentRunInput, AgentRunResult, InjectMessageCommand


@pytest.mark.asyncio
async def test_gateway_dispatches_run_turn_and_updates_activity(tmp_path):
    core = AsyncMock()
    core.run_turn = AsyncMock(return_value=AgentRunResult(output_text="ok"))
    core.get_session_state = MagicMock(return_value=MagicMock(turn_count=1))
    core.get_token_usage = MagicMock(return_value={"total_tokens": 3})

    gateway = AutomationCoreGateway(
        core,
        session_id="cli:default",
        session_registry=SessionRegistry(str(tmp_path / "sessions.db")),
    )
    result = await gateway.run_turn(AgentRunInput(text="hello"), hooks=AgentHooks())

    assert result.output_text == "ok"
    core.run_turn.assert_awaited_once()
    call = core.run_turn.await_args
    assert call.args[0].text == "hello"
    assert call.args[0].metadata["session_id"] == "cli:default"


@pytest.mark.asyncio
async def test_gateway_expire_flow_calls_finalize_then_reset(tmp_path):
    core = AsyncMock()
    core.finalize_session = AsyncMock(return_value=None)
    core.reset_session = MagicMock()
    core.run_turn = AsyncMock(return_value=AgentRunResult(output_text="ok"))
    core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    gateway = AutomationCoreGateway(
        core,
        policy=SessionCutPolicy(idle_timeout_minutes=0, daily_cutoff_hour=4),
        session_registry=SessionRegistry(str(tmp_path / "sessions.db")),
    )
    changed = await gateway.expire_session_if_needed(reason="idle_timeout")

    assert changed is True
    core.finalize_session.assert_awaited_once()
    core.reset_session.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_expire_still_resets_when_finalize_fails(tmp_path):
    core = AsyncMock()
    core.finalize_session = AsyncMock(side_effect=RuntimeError("boom"))
    core.reset_session = MagicMock()
    core.run_turn = AsyncMock(return_value=AgentRunResult(output_text="ok"))
    core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    gateway = AutomationCoreGateway(core, session_registry=SessionRegistry(str(tmp_path / "sessions.db")))
    await gateway.expire_session(reason="manual")

    core.finalize_session.assert_awaited_once()
    core.reset_session.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_inject_message_forwards_command_metadata(tmp_path):
    core = AsyncMock()
    core.run_turn = AsyncMock(return_value=AgentRunResult(output_text="default"))
    core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    core_external = AsyncMock()
    core_external.run_turn = AsyncMock(return_value=AgentRunResult(output_text="ok"))
    core_external.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    factory = AsyncMock(return_value=core_external)

    gateway = AutomationCoreGateway(
        core,
        session_id="cli:default",
        session_factory=factory,
        session_registry=SessionRegistry(str(tmp_path / "sessions.db")),
    )
    await gateway.inject_message(
        command=InjectMessageCommand(
            session_id="wechat:user-1",
            input=AgentRunInput(text="hello", metadata={"from_input": "1", "trace_id": "input", "session_id": "bad-input"}),
            metadata={"from_command": "2", "trace_id": "command", "session_id": "bad-command"},
        ),
        hooks=AgentHooks(),
    )

    call = core_external.run_turn.await_args
    assert call.args[0].metadata["session_id"] == "wechat:user-1"
    assert call.args[0].metadata["from_input"] == "1"
    assert call.args[0].metadata["from_command"] == "2"
    assert call.args[0].metadata["trace_id"] == "command"


@pytest.mark.asyncio
async def test_gateway_switch_session_creates_and_routes_to_new_core(tmp_path):
    core_default = AsyncMock()
    core_default.run_turn = AsyncMock(return_value=AgentRunResult(output_text="default"))
    core_default.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    created_core = AsyncMock()
    created_core.run_turn = AsyncMock(return_value=AgentRunResult(output_text="new"))
    created_core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    created_core.activate_session = AsyncMock(return_value=None)
    factory = AsyncMock(return_value=created_core)

    gateway = AutomationCoreGateway(
        core_default,
        session_id="cli:default",
        session_factory=factory,
        session_registry=SessionRegistry(str(tmp_path / "sessions.db")),
    )
    created = await gateway.switch_session("cli:work")
    result = await gateway.run_turn(AgentRunInput(text="hello"), hooks=AgentHooks())

    assert created is True
    assert result.output_text == "new"
    factory.assert_awaited_once_with("cli:work")
    created_core.activate_session.assert_awaited_once_with("cli:work")
    created_core.run_turn.assert_awaited_once()
    assert "cli:default" in gateway.list_sessions()
    assert "cli:work" in gateway.list_sessions()


@pytest.mark.asyncio
async def test_gateway_inject_message_uses_target_session_without_switching_active(tmp_path):
    core_default = AsyncMock()
    core_default.run_turn = AsyncMock(return_value=AgentRunResult(output_text="default"))
    core_default.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    created_core = AsyncMock()
    created_core.run_turn = AsyncMock(return_value=AgentRunResult(output_text="other"))
    created_core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    factory = AsyncMock(return_value=created_core)

    gateway = AutomationCoreGateway(
        core_default,
        session_id="cli:default",
        session_factory=factory,
        session_registry=SessionRegistry(str(tmp_path / "sessions.db")),
    )
    await gateway.inject_message(
        InjectMessageCommand(session_id="wx:u1", input=AgentRunInput(text="push")),
        hooks=AgentHooks(),
    )
    result = await gateway.run_turn(AgentRunInput(text="local"), hooks=AgentHooks())

    assert gateway.active_session_id == "cli:default"
    assert result.output_text == "default"
    created_core.run_turn.assert_awaited_once()
    core_default.run_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_close_only_closes_owned_sessions(tmp_path):
    core_default = AsyncMock()
    core_default.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    core_default.run_turn = AsyncMock(return_value=AgentRunResult(output_text="default"))
    core_default.close = AsyncMock()

    created_core = AsyncMock()
    created_core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    created_core.run_turn = AsyncMock(return_value=AgentRunResult(output_text="new"))
    created_core.close = AsyncMock()
    factory = AsyncMock(return_value=created_core)

    gateway = AutomationCoreGateway(
        core_default,
        session_id="cli:default",
        session_factory=factory,
        session_registry=SessionRegistry(str(tmp_path / "sessions.db")),
    )
    await gateway.switch_session("cli:work")
    await gateway.close()

    core_default.close.assert_not_awaited()
    created_core.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_sessions_visible_across_instances(tmp_path):
    db_path = str(tmp_path / "sessions.db")
    core_a = AsyncMock()
    core_a.run_turn = AsyncMock(return_value=AgentRunResult(output_text="a"))
    core_a.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    core_a2 = AsyncMock()
    core_a2.run_turn = AsyncMock(return_value=AgentRunResult(output_text="a2"))
    core_a2.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    factory = AsyncMock(return_value=core_a2)

    core_b = AsyncMock()
    core_b.run_turn = AsyncMock(return_value=AgentRunResult(output_text="b"))
    core_b.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))

    gw_a = AutomationCoreGateway(
        core_a,
        session_id="cli:default",
        session_registry=SessionRegistry(db_path),
        owner_id="root",
        source="cli",
        session_factory=factory,
    )
    await gw_a.switch_session("cli:work")
    await gw_a.close()

    gw_b = AutomationCoreGateway(
        core_b,
        session_id="cli:default",
        session_registry=SessionRegistry(db_path),
        owner_id="root",
        source="cli",
    )
    sessions = gw_b.list_sessions()
    await gw_b.close()

    assert "cli:default" in sessions
    assert "cli:work" in sessions
