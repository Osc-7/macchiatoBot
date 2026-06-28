"""Goal slash command 与 IPC 路径测试。"""

from __future__ import annotations

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
    client.create_goal.assert_awaited_once_with("fix the auth bug", autostart=True)
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
