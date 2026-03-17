"""Automation IPC tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from system.automation import (
    AutomationCoreGateway,
    AutomationIPCClient,
    AutomationIPCServer,
    SessionRegistry,
)
from system.kernel import CorePool
from agent_core.interfaces import AgentHooks, AgentRunInput, AgentRunResult


def _make_ipc_mock_scheduler(work_result: AgentRunResult, work_usage: dict):
    """创建支持 hooks 调用的 mock scheduler，用于 IPC 测试。"""
    last_request = []

    async def _submit(request):
        last_request.append(request)
        sid = getattr(request, "session_id", "cli:default")

        class Handle:
            request_id = getattr(request, "request_id", "mock-req-id")
            session_id = sid

            def __await__(self):
                return _wait_result(self).__await__()

        return Handle()

    async def _wait_result(handle):
        req = last_request[-1] if last_request else None
        metadata = getattr(req, "metadata", {}) or {}
        hooks = metadata.get("_hooks")
        if hooks:
            if hooks.on_trace_event:
                await hooks.on_trace_event(
                    {"type": "llm_request", "iteration": 1, "tool_count": 3}
                )
            if hooks.on_reasoning_delta:
                await hooks.on_reasoning_delta("thinking...")
            if hooks.on_assistant_delta:
                await hooks.on_assistant_delta("hello ")
                await hooks.on_assistant_delta("world")
        return work_result

    work_agent = MagicMock()
    work_agent.get_token_usage = MagicMock(return_value=work_usage)
    work_agent.get_turn_count = MagicMock(return_value=1)
    work_agent.clear_context = MagicMock()
    work_entry = MagicMock()
    work_entry.agent = work_agent

    mock_pool = MagicMock(spec=CorePool)
    mock_pool.list_sessions = MagicMock(return_value=["cli:default", "cli:work"])
    # has_session 返回 False，使 switch_session 时 created=True（session 由 gateway 创建）
    mock_pool.has_session = MagicMock(return_value=False)
    mock_pool.evict = AsyncMock()
    mock_pool.get_entry = MagicMock(
        side_effect=lambda sid: work_entry if sid == "cli:work" else None
    )

    mock_scheduler = MagicMock()
    mock_scheduler.submit = AsyncMock(side_effect=_submit)
    mock_scheduler.wait_result = AsyncMock(side_effect=_wait_result)
    mock_scheduler.core_pool = mock_pool
    mock_scheduler.subscribe_out = MagicMock(return_value="mock-sub-id")
    mock_scheduler.unsubscribe_out = MagicMock()

    return mock_scheduler, work_agent


@pytest.mark.asyncio
async def test_ipc_server_client_run_turn_and_session_commands(tmp_path: Path):
    default_core = AsyncMock()
    default_core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    default_core.close = AsyncMock()

    work_result = AgentRunResult(output_text="work-ok")
    work_usage = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "call_count": 1,
        "cost_yuan": 0.0,
    }
    scheduler, work_agent = _make_ipc_mock_scheduler(work_result, work_usage)

    gateway = AutomationCoreGateway(
        default_core,
        kernel_scheduler=scheduler,
        session_id="cli:default",
        session_registry=SessionRegistry(str(tmp_path / "sessions.db")),
    )

    socket_path = str(tmp_path / "automation.sock")
    server = AutomationIPCServer(
        gateway, owner_id="root", source="cli", socket_path=socket_path
    )
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
        assistant_deltas: list[str] = []
        reasoning_deltas: list[str] = []

        async def _on_trace_event(evt: dict) -> None:
            trace_events.append(evt)

        async def _on_assistant_delta(delta: str) -> None:
            assistant_deltas.append(delta)

        async def _on_reasoning_delta(delta: str) -> None:
            reasoning_deltas.append(delta)

        result = await client.run_turn(
            AgentRunInput(text="hello"),
            hooks=AgentHooks(
                on_trace_event=_on_trace_event,
                on_assistant_delta=_on_assistant_delta,
                on_reasoning_delta=_on_reasoning_delta,
            ),
        )
        assert result.output_text == "work-ok"
        assert isinstance(trace_events, list)
        assert "".join(assistant_deltas) == "hello world"
        assert reasoning_deltas == ["thinking..."]

        usage = await client.get_token_usage()
        assert usage["total_tokens"] == 15

        await client.clear_context()
        work_agent.clear_context.assert_called_once()
    finally:
        await client.close()
        await server.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_ipc_session_delete_rejected_when_session_is_active_for_any_client(
    tmp_path: Path,
):
    from tests.test_automation_core_gateway import _make_mock_scheduler

    default_core = AsyncMock()
    default_core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    default_core.close = AsyncMock()

    work_core = AsyncMock()
    work_core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    work_core.activate_session = AsyncMock(return_value=None)
    work_core.delete_session_history = MagicMock(return_value=1)
    work_core.close = AsyncMock()
    factory = AsyncMock(return_value=work_core)

    scheduler = _make_mock_scheduler()
    gateway = AutomationCoreGateway(
        default_core,
        kernel_scheduler=scheduler,
        session_id="cli:default",
        session_factory=factory,
        session_registry=SessionRegistry(str(tmp_path / "sessions.db")),
    )
    socket_path = str(tmp_path / "automation.sock")
    server = AutomationIPCServer(
        gateway, owner_id="root", source="cli", socket_path=socket_path
    )
    await server.start()
    client_a = AutomationIPCClient(
        owner_id="root", source="cli", socket_path=socket_path
    )
    client_b = AutomationIPCClient(
        owner_id="root", source="cli", socket_path=socket_path
    )
    try:
        await client_a.connect()
        await client_b.connect()
        await client_a.switch_session("cli:work", create_if_missing=True)
        await client_b.switch_session("cli:work", create_if_missing=True)

        deleted = await client_a.delete_session("cli:work")

        assert deleted is False
        work_core.delete_session_history.assert_not_called()
    finally:
        await client_a.close()
        await client_b.close()
        await server.stop()
        await gateway.close()
