"""Goal slash command 与 IPC 路径测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.agent.agent import AgentCore
from agent_core.config import get_config
from frontend.feishu.slash_commands import (
    _format_goal_create_result,
    _format_goal_list_result,
    try_handle_slash_command,
)


def test_format_goal_create_result() -> None:
    text = _format_goal_create_result(
        {
            "goal": {"id": "goal-abc", "title": "写报告"},
            "autostart_queued": True,
        }
    )
    assert "goal-abc" in text
    assert "已开始执行" in text


def test_format_goal_list_result_empty() -> None:
    assert "没有活跃" in _format_goal_list_result({"goals": []})


@pytest.mark.asyncio
async def test_slash_goal_help() -> None:
    client = MagicMock()
    handled, reply = await try_handle_slash_command(client, "/goal help")
    assert handled is True
    assert reply and "/goal <instruction>" in reply


@pytest.mark.asyncio
async def test_slash_goal_create() -> None:
    client = MagicMock()
    client.create_goal = AsyncMock(
        return_value={
            "goal": {"id": "goal-x", "title": "fix bug"},
            "autostart_queued": True,
        }
    )
    handled, reply = await try_handle_slash_command(client, "/goal fix the auth bug")
    assert handled is True
    client.create_goal.assert_awaited_once_with(
        "fix the auth bug", autostart=True, feishu_chat_id=None
    )
    assert reply and "goal-x" in reply


@pytest.mark.asyncio
async def test_slash_goal_list() -> None:
    client = MagicMock()
    client.list_goals = AsyncMock(
        return_value={
            "goals": [{"id": "g1", "title": "t1", "status": "active", "steps": []}],
        }
    )
    handled, reply = await try_handle_slash_command(client, "/goal list")
    assert handled is True
    client.list_goals.assert_awaited_once()
    assert reply and "g1" in reply


def test_agent_create_user_goal() -> None:
    agent = AgentCore(config=get_config(), tools=[], memory_enabled=False)
    goal = agent.create_user_goal("重构模块")
    assert goal["title"] == "重构模块"
    assert agent._goal_store.has_active_goals()


@pytest.mark.asyncio
async def test_slash_command_via_ipc_registers_feishu_push(monkeypatch) -> None:
    from frontend.feishu import ipc_bridge

    registered: list[tuple[str, str, float]] = []

    class _Forwarder:
        def start(self) -> None:
            pass

        def register(self, session_id: str, chat_id: str, ttl_seconds: float = 300.0) -> None:
            registered.append((session_id, chat_id, ttl_seconds))

    monkeypatch.setattr(ipc_bridge, "get_feishu_push_forwarder", lambda: _Forwarder())
    monkeypatch.setattr(
        ipc_bridge,
        "register_feishu_push_session",
        lambda session_id, chat_id, ttl_seconds=300.0: registered.append(
            (session_id, chat_id, ttl_seconds)
        ),
    )

    client = MagicMock()
    client.ping = AsyncMock(return_value=True)
    client.switch_session = AsyncMock()
    client.active_session_id = "feishu:user:ou_test:123"
    client.create_goal = AsyncMock(
        return_value={
            "goal": {"id": "goal-x", "title": "watch training"},
            "autostart_queued": True,
        }
    )

    def _fake_ipc_client(**kwargs):
        return client

    monkeypatch.setattr(ipc_bridge, "AutomationIPCClient", _fake_ipc_client)
    monkeypatch.setattr(
        ipc_bridge,
        "try_handle_slash_command",
        AsyncMock(return_value=(True, "已创建目标 goal-x")),
    )

    reply = await ipc_bridge.try_handle_slash_command_via_ipc(
        session_id="feishu:user:ou_test:123",
        text="/goal watch training",
        feishu_chat_id="oc_test_chat",
    )
    assert reply == "已创建目标 goal-x"
    assert registered
    assert registered[0][0] == "feishu:user:ou_test:123"
    assert registered[0][1] == "oc_test_chat"

@pytest.mark.asyncio
async def test_create_goal_for_feishu_passes_chat_id_hint(monkeypatch) -> None:
    from system.automation.core_gateway import AutomationCoreGateway

    feishu_sid = "feishu:user:ou_test:999"
    captured: dict = {}

    mock_agent = MagicMock()
    mock_agent.create_user_goal = MagicMock(
        return_value={"id": "goal-abc", "title": "task"}
    )
    mock_agent._finalize_turn = AsyncMock()

    mock_entry = MagicMock()
    mock_pool = MagicMock()
    mock_pool.acquire = AsyncMock(return_value=mock_agent)
    mock_pool.get_entry = MagicMock(return_value=mock_entry)

    mock_scheduler = MagicMock()
    mock_scheduler.core_pool = mock_pool

    @asynccontextmanager
    async def _fake_hold_lock(sid: str):
        yield

    mock_scheduler.hold_session_lock = _fake_hold_lock
    mock_scheduler.inject_turn = MagicMock(
        side_effect=lambda req: captured.update({"request": req})
    )

    gateway = AutomationCoreGateway(
        MagicMock(),
        kernel_scheduler=mock_scheduler,
        session_id="cli:root",
        owner_id="root",
        source="cli",
    )
    gateway.ensure_session = AsyncMock()
    gateway._ensure_subscribed = MagicMock()
    gateway.mark_activity = MagicMock()

    build_calls: list = []

    def _fake_build(sid, pool, **kw):
        build_calls.append(kw)
        return {"feishu_chat_id": "oc_chat", "_hooks": object()}

    monkeypatch.setattr(
        "agent_core.tools.bash_job_notify.build_feishu_inject_metadata",
        _fake_build,
    )

    await gateway.create_goal_for_session(
        feishu_sid, "task", autostart=True, feishu_chat_id="oc_chat"
    )
    assert build_calls and build_calls[0].get("chat_id_hint") == "oc_chat"
    assert mock_entry.feishu_chat_id == "oc_chat"
    req = captured["request"]
    assert req.metadata.get("feishu_chat_id") == "oc_chat"


@pytest.mark.asyncio
async def test_slash_goal_create_passes_feishu_chat_id() -> None:
    client = MagicMock()
    client.feishu_chat_id = "oc_goal_chat"
    client.create_goal = AsyncMock(
        return_value={
            "goal": {"id": "goal-y", "title": "watch"},
            "autostart_queued": True,
        }
    )
    handled, reply = await try_handle_slash_command(
        client, "/goal watch training progress"
    )
    assert handled is True
    client.create_goal.assert_awaited_once_with(
        "watch training progress",
        autostart=True,
        feishu_chat_id="oc_goal_chat",
    )
    assert reply and "goal-y" in reply


@pytest.mark.asyncio
async def test_create_goal_for_feishu_session_injects_feishu_metadata(monkeypatch) -> None:
    from system.automation.core_gateway import AutomationCoreGateway

    feishu_sid = "feishu:user:ou_test:999"
    captured: dict = {}

    mock_agent = MagicMock()
    mock_agent.create_user_goal = MagicMock(
        return_value={"id": "goal-abc", "title": "task"}
    )
    mock_agent._finalize_turn = AsyncMock()

    mock_pool = MagicMock()
    mock_pool.acquire = AsyncMock(return_value=mock_agent)

    mock_scheduler = MagicMock()
    mock_scheduler.core_pool = mock_pool

    @asynccontextmanager
    async def _fake_hold_lock(sid: str):
        yield

    mock_scheduler.hold_session_lock = _fake_hold_lock

    def _capture_inject(request):
        captured["request"] = request

    mock_scheduler.inject_turn = MagicMock(side_effect=_capture_inject)

    gateway = AutomationCoreGateway(
        MagicMock(),
        kernel_scheduler=mock_scheduler,
        session_id="cli:root",
        owner_id="root",
        source="cli",
    )
    gateway.ensure_session = AsyncMock()
    gateway._ensure_subscribed = MagicMock()
    gateway.mark_activity = MagicMock()

    monkeypatch.setattr(
        "agent_core.tools.bash_job_notify.build_feishu_inject_metadata",
        lambda sid, pool, **kw: {"feishu_chat_id": "oc_chat", "_hooks": object()},
    )

    res = await gateway.create_goal_for_session(
        feishu_sid, "task", autostart=True, feishu_chat_id="oc_chat"
    )
    assert res["autostart_queued"] is True
    mock_pool.acquire.assert_awaited_once_with(
        feishu_sid,
        source="feishu",
        user_id="ou_test",
        create_if_missing=True,
    )
    req = captured["request"]
    assert req.session_id == feishu_sid
    assert req.metadata["source"] == "feishu"
    assert req.metadata["kind"] == "goal_start"
    assert req.metadata.get("feishu_chat_id") == "oc_chat"
