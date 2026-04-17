"""飞书斜杠指令测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from frontend.feishu.slash_commands import (
    _format_token_usage,
    _help_text,
    try_handle_slash_command,
)


def test_help_text():
    h = _help_text()
    assert "/clear" in h
    assert "/usage" in h
    assert "/model" in h
    assert "/session" in h
    assert "/help" in h


def test_format_token_usage():
    u = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "call_count": 2,
        "cost_yuan": 0.001,
    }
    out = _format_token_usage(u)
    assert "100" in out
    assert "150" in out
    assert "2" in out
    assert "0.001" in out


@pytest.mark.asyncio
async def test_try_handle_slash_command_help():
    client = MagicMock()
    handled, reply = await try_handle_slash_command(client, "/help")
    assert handled is True
    assert reply is not None
    assert "可用指令" in reply


@pytest.mark.asyncio
async def test_try_handle_slash_command_not_command():
    client = MagicMock()
    handled, reply = await try_handle_slash_command(client, "明天8点开会")
    assert handled is False
    assert reply is None


@pytest.mark.asyncio
async def test_try_handle_slash_command_clear():
    client = MagicMock()
    client.clear_context = AsyncMock()
    handled, reply = await try_handle_slash_command(client, "/clear")
    assert handled is True
    assert "清空" in (reply or "")
    client.clear_context.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_handle_slash_command_usage():
    client = MagicMock()
    client.get_token_usage = AsyncMock(
        return_value={
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "call_count": 1,
        }
    )
    handled, reply = await try_handle_slash_command(client, "/usage")
    assert handled is True
    assert "150" in (reply or "")
    assert "1" in (reply or "")


@pytest.mark.asyncio
async def test_try_handle_slash_command_model_list():
    client = MagicMock()
    client.list_models = AsyncMock(
        return_value=[
            {
                "name": "kimi_k25",
                "api_model": "kimi-k2.5",
                "label": "Kimi K2.5",
                "is_active": True,
                "is_vision_provider": False,
                "vision": True,
                "function_calling": True,
            }
        ]
    )
    handled, reply = await try_handle_slash_command(client, "/model")
    assert handled is True
    assert reply is not None
    assert "Kimi K2.5" in reply
    assert "vision,tools" in (reply or "")
    client.list_models.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_handle_slash_command_model_switch():
    client = MagicMock()
    client.switch_model = AsyncMock(
        return_value={
            "name": "kimi_k25",
            "api_model": "kimi-k2.5",
            "vision": True,
            "vision_provider": "qwen_dashscope",
        }
    )
    handled, reply = await try_handle_slash_command(
        client, "/model Kimi K2.5"
    )
    assert handled is True
    assert reply is not None
    assert "kimi_k25" in reply
    client.switch_model.assert_awaited_once_with("Kimi K2.5")
