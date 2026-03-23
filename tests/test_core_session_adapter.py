"""CoreSession 适配器测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.adapters import CoreSessionAdapter
from agent_core.interfaces import AgentHooks, AgentRunInput
from agent_core.kernel_interface import ReturnAction


@pytest.mark.asyncio
async def test_run_turn_forwards_callbacks_and_events():
    """使用 kernel 兼容的 mock agent 验证回调与事件转发。"""
    agent = MagicMock()
    agent._current_turn_id = 0
    agent._llm_client = MagicMock()
    agent._tool_registry = MagicMock()
    agent._session_id = "sess-1"

    async def _run_loop(turn_id=0, hooks=None):
        if hooks and hooks.on_assistant_delta:
            await hooks.on_assistant_delta("A")
        if hooks and hooks.on_reasoning_delta:
            await hooks.on_reasoning_delta("R")
        if hooks and hooks.on_trace_event:
            await hooks.on_trace_event({"type": "tool_call", "name": "x"})
            await hooks.on_trace_event({"type": "tool_result", "name": "x", "success": True})
        yield ReturnAction(message="done", status="completed")

    agent.run_loop = _run_loop
    agent.prepare_turn = AsyncMock(return_value=1)
    agent._finalize_turn = AsyncMock()
    agent.finalize_session = AsyncMock()
    agent.reset_session = MagicMock()
    agent.close = AsyncMock()
    agent.get_turn_count = MagicMock(return_value=3)
    agent.get_token_usage = MagicMock(return_value={"total_tokens": 10})

    adapter = CoreSessionAdapter(agent)

    stream_cb = AsyncMock()
    reasoning_cb = AsyncMock()
    trace_cb = AsyncMock()
    event_cb = AsyncMock()
    hooks = AgentHooks(
        on_assistant_delta=stream_cb,
        on_reasoning_delta=reasoning_cb,
        on_trace_event=trace_cb,
        on_event=event_cb,
    )

    result = await adapter.run_turn(AgentRunInput(text="hello"), hooks=hooks)

    assert result.output_text == "done"
    stream_cb.assert_awaited()
    reasoning_cb.assert_awaited()
    assert trace_cb.await_count == 2
    event_names = [call.args[0].name for call in event_cb.await_args_list]
    assert "agent_start" in event_names
    assert "turn_start" in event_names
    assert "assistant_delta" in event_names
    assert "reasoning_delta" in event_names
    assert "tool_call" in event_names
    assert "tool_result" in event_names
    assert "assistant_final" in event_names
    assert "turn_end" in event_names


@pytest.mark.asyncio
async def test_session_lifecycle_passthrough():
    agent = MagicMock()
    agent.finalize_session = AsyncMock(return_value="summary")
    agent.reset_session = MagicMock()
    agent.close = AsyncMock()
    agent.get_turn_count = MagicMock(return_value=2)
    agent.get_token_usage = MagicMock(return_value={"total_tokens": 5})
    agent._session_id = "sess-2"

    adapter = CoreSessionAdapter(agent)

    summary = await adapter.finalize_session()
    adapter.reset_session()
    state = adapter.get_session_state()
    await adapter.close()

    assert summary == "summary"
    agent.reset_session.assert_called_once()
    assert state.session_id == "sess-2"
    assert state.turn_count == 2
    assert state.token_usage["total_tokens"] == 5
    agent.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_activate_session_passthrough():
    agent = MagicMock()
    agent.finalize_session = AsyncMock(return_value=None)
    agent.reset_session = MagicMock()
    agent.close = AsyncMock()
    agent.get_turn_count = MagicMock(return_value=0)
    agent.get_token_usage = MagicMock(return_value={"total_tokens": 0})
    agent.activate_session = AsyncMock(return_value=None)

    adapter = CoreSessionAdapter(agent)
    await adapter.activate_session("cli:shared")

    agent.activate_session.assert_awaited_once_with(
        "cli:shared", replay_messages_limit=None
    )
