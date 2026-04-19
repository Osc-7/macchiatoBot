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
    assert "/compress" in h
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
async def test_try_handle_slash_command_compress_default():
    client = MagicMock()
    client.compress_context = AsyncMock(
        return_value={
            "compressed": True,
            "summary": "用户与助手讨论 X",
            "summary_chars": 10,
            "messages_before": 12,
            "messages_after": 4,
            "kept": 4,
            "current_tokens": 18000,
            "threshold_tokens": 12000,
            "compression_round": 1,
            "model": "kimi-k2.5",
            "session_loaded": True,
        }
    )
    handled, reply = await try_handle_slash_command(client, "/compress")
    assert handled is True
    assert reply is not None
    assert "已压缩" in reply
    assert "12 → 4" in reply
    assert "18,000" in reply  # current tokens 千分位
    assert "12,000" in reply  # 阈值
    assert "kimi-k2.5" in reply
    client.compress_context.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_try_handle_slash_command_compress_with_keep_recent():
    client = MagicMock()
    client.compress_context = AsyncMock(
        return_value={
            "compressed": True,
            "messages_before": 20,
            "messages_after": 5,
            "kept": 5,
            "current_tokens": 30000,
            "threshold_tokens": 20000,
            "summary_chars": 200,
            "compression_round": 2,
            "model": "qwen-plus",
            "session_loaded": True,
        }
    )
    handled, reply = await try_handle_slash_command(client, "/compress 2")
    assert handled is True
    assert "保留 5 条" in (reply or "")
    client.compress_context.assert_awaited_once_with(2)


@pytest.mark.asyncio
async def test_try_handle_slash_command_compress_invalid_keep():
    client = MagicMock()
    client.compress_context = AsyncMock()
    handled, reply = await try_handle_slash_command(client, "/compress abc")
    assert handled is True
    assert "用法" in (reply or "")
    client.compress_context.assert_not_called()


@pytest.mark.asyncio
async def test_try_handle_slash_command_compress_session_not_loaded():
    """daemon 内 session 未驻留时应给出友好提示，而不是裸数字。"""
    client = MagicMock()
    client.compress_context = AsyncMock(
        return_value={
            "compressed": False,
            "messages_before": 0,
            "messages_after": 0,
            "session_loaded": False,
        }
    )
    handled, reply = await try_handle_slash_command(client, "/compress")
    assert handled is True
    assert reply is not None
    assert "未在 daemon 内驻留" in reply


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
