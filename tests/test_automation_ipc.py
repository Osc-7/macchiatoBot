"""Automation IPC tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_agent.automation import (
    AutomationCoreGateway,
    AutomationIPCClient,
    AutomationIPCServer,
    SessionRegistry,
)
from schedule_agent.core.interfaces import AgentHooks, AgentRunInput, AgentRunResult


@pytest.mark.asyncio
async def test_ipc_server_client_run_turn_and_session_commands(tmp_path: Path):
    default_core = AsyncMock()
    default_core.run_turn = AsyncMock(return_value=AgentRunResult(output_text="default"))
    default_core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    default_core.get_token_usage = MagicMock(
        return_value={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "call_count": 0,
            "cost_yuan": 0.0,
        }
    )
    default_core.clear_context = MagicMock()
    default_core.close = AsyncMock()

    work_core = AsyncMock()
    work_core.run_turn = AsyncMock(return_value=AgentRunResult(output_text="work-ok"))
    work_core.get_session_state = MagicMock(return_value=MagicMock(turn_count=1))
    work_core.get_token_usage = MagicMock(
        return_value={
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "call_count": 1,
            "cost_yuan": 0.0,
        }
    )
    work_core.clear_context = MagicMock()
    work_core.activate_session = AsyncMock(return_value=None)
    work_core.close = AsyncMock()

    factory = AsyncMock(return_value=work_core)
    gateway = AutomationCoreGateway(
        default_core,
        session_id="cli:default",
        session_factory=factory,
        session_registry=SessionRegistry(str(tmp_path / "sessions.db")),
    )

    socket_path = str(tmp_path / "automation.sock")
    server = AutomationIPCServer(gateway, owner_id="root", source="cli", socket_path=socket_path)
    await server.start()
    client = AutomationIPCClient(owner_id="root", source="cli", socket_path=socket_path)
    try:
        assert await client.ping() is True
        await client.connect()

        sessions = await client.list_sessions()
        assert "cli:default" in sessions

        created = await client.switch_session("cli:work", create_if_missing=True)
        assert created is True
        assert client.active_session_id == "cli:work"

        trace_events: list[dict] = []

        async def _on_trace_event(evt: dict) -> None:
            trace_events.append(evt)

        result = await client.run_turn(
            AgentRunInput(text="hello"),
            hooks=AgentHooks(on_trace_event=_on_trace_event),
        )
        assert result.output_text == "work-ok"
        assert isinstance(trace_events, list)

        usage = await client.get_token_usage()
        assert usage["total_tokens"] == 15

        await client.clear_context()
        work_core.clear_context.assert_called_once()
    finally:
        await client.close()
        await server.stop()
        await gateway.close()

