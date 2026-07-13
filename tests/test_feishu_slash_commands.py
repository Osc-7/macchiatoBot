"""飞书斜杠指令测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from frontend.feishu import slash_commands as slash_commands_module
from frontend.feishu.slash_commands import (
    _format_token_usage,
    _help_text,
    try_handle_slash_command,
)


def test_help_text():
    h = _help_text()
    assert "/clear" in h
    assert "/interrupt" in h
    assert "/compress" in h
    assert "/usage" in h
    assert "/model" in h
    assert "/session" in h
    assert "/new" in h
    assert "/remote-use" in h
    assert "/skill" in h
    assert "/dangerously" in h
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


def test_format_token_usage_includes_cache_when_present():
    u = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "call_count": 1,
        "prompt_cache_hit_tokens": 80,
        "prompt_cache_miss_tokens": 20,
    }
    out = _format_token_usage(u)
    assert "缓存命中" in out
    assert "80" in out
    assert "20" in out


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
async def test_try_handle_slash_command_interrupt():
    client = MagicMock()
    client.active_session_id = "feishu:ou_test"
    client.terminal_cancel = AsyncMock(return_value=True)
    handled, reply = await try_handle_slash_command(client, "/interrupt")
    assert handled is True
    assert "Chat session interrupted." in (reply or "")
    client.terminal_cancel.assert_awaited_once_with("feishu:ou_test")


@pytest.mark.asyncio
async def test_try_handle_slash_command_cancel_idle():
    client = MagicMock()
    client.active_session_id = "feishu:ou_x"
    client.terminal_cancel = AsyncMock(return_value=False)
    handled, reply = await try_handle_slash_command(client, "/cancel")
    assert handled is True
    assert "No active chat session." in (reply or "")


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
    handled, reply = await try_handle_slash_command(client, "/model Kimi K2.5")
    assert handled is True
    assert reply is not None
    assert "kimi_k25" in reply
    client.switch_model.assert_awaited_once_with("Kimi K2.5")


@pytest.mark.asyncio
async def test_try_handle_slash_command_new_alias(monkeypatch):
    client = MagicMock()
    client.active_session_id = "feishu:user:ou_test"
    client.feishu_base_session_id = "feishu:user:ou_test"
    client.expire_session = AsyncMock(return_value=True)
    client.switch_session = AsyncMock(return_value=True)
    monkeypatch.setattr(slash_commands_module.time, "time", lambda: 1234)

    handled, reply = await try_handle_slash_command(client, "/new")

    assert handled is True
    assert "已创建并切换到新会话" in (reply or "")
    client.expire_session.assert_awaited_once_with(
        "feishu:user:ou_test", reason="manual_new"
    )
    client.switch_session.assert_awaited_once_with(
        "feishu:user:ou_test:1234", create_if_missing=True
    )


@pytest.mark.asyncio
async def test_try_handle_slash_command_session_list_scoped_to_feishu_window():
    client = MagicMock()
    client.active_session_id = "feishu:legacy-active"
    client.feishu_base_session_id = "feishu:user:ou_me"
    client.session_list_limit = 30
    client.list_sessions = AsyncMock(
        return_value=[
            "cli:default",
            "cron:job-1",
            "feishu:user:ou_me",
            "feishu:user:ou_me:123",
            "feishu:user:ou_other",
            "feishu:legacy-active",
            "shuiyuan:alice",
        ]
    )

    handled, reply = await try_handle_slash_command(client, "/session list")

    assert handled is True
    assert reply is not None
    assert "当前飞书窗口" in reply
    assert "feishu:user:ou_me" in reply
    assert "feishu:user:ou_me:123" in reply
    assert "feishu:legacy-active *" in reply
    assert "cli:default" not in reply
    assert "cron:job-1" not in reply
    assert "shuiyuan:alice" not in reply
    assert "feishu:user:ou_other" not in reply


@pytest.mark.asyncio
async def test_try_handle_slash_command_remote_use():
    client = MagicMock()
    client.remote_workspace_use = AsyncMock(
        return_value={
            "login": "personal",
            "requested_path": "~/Project",
            "profile": "dev",
            "workspace_mount": "/workspace",
        }
    )
    handled, reply = await try_handle_slash_command(
        client, "/remote-use personal ~/Project --profile dev --ttl 30m"
    )
    assert handled is True
    assert reply is not None
    assert "远程工作区已启用" in reply
    assert "personal" in reply
    client.remote_workspace_use.assert_awaited_once_with(
        login="personal",
        path="~/Project",
        profile="dev",
        ttl_seconds=1800,
    )


@pytest.mark.asyncio
async def test_try_handle_slash_command_remote_status_inactive():
    client = MagicMock()
    client.remote_workspace_status = AsyncMock(
        return_value={"active": False, "state": None}
    )
    handled, reply = await try_handle_slash_command(client, "/remote-status")
    assert handled is True
    assert "未启用" in (reply or "")


@pytest.mark.asyncio
async def test_try_handle_slash_command_remote_release():
    client = MagicMock()
    client.remote_workspace_release = AsyncMock(
        return_value={"released": True, "state": {"login": "personal"}}
    )
    handled, reply = await try_handle_slash_command(client, "/remote-release")
    assert handled is True
    assert "已释放" in (reply or "")


@pytest.mark.asyncio
async def test_try_handle_slash_command_skill_loads():
    client = MagicMock()
    client.load_skill = AsyncMock(
        return_value={
            "ok": True,
            "skill_name": "demo",
            "injected": True,
            "backend": "remote",
            "error": None,
            "message": "Loaded skill `demo`.",
        }
    )
    handled, reply = await try_handle_slash_command(client, "/skill demo")
    assert handled is True
    assert "demo" in (reply or "")
    assert "远程" in (reply or "")
    client.load_skill.assert_awaited_once_with(skill_name="demo")


@pytest.mark.asyncio
async def test_try_handle_slash_command_skill_bare_lists():
    client = MagicMock()
    client.list_skills = AsyncMock(
        return_value={
            "ok": True,
            "backend": "local",
            "index": "- **Demo** (`demo`): hello",
        }
    )
    client.load_skill = AsyncMock()
    handled, reply = await try_handle_slash_command(client, "/skill")
    assert handled is True
    assert "可用技能" in (reply or "")
    assert "demo" in (reply or "")
    client.list_skills.assert_awaited_once()
    client.load_skill.assert_not_called()


@pytest.mark.asyncio
async def test_try_handle_slash_command_skill_named_list_loads():
    """`/skill list` must load a skill named list, not open a list subcommand."""
    client = MagicMock()
    client.load_skill = AsyncMock(
        return_value={
            "ok": True,
            "skill_name": "list",
            "injected": True,
            "backend": "local",
            "error": None,
            "message": "Loaded skill `list`.",
        }
    )
    client.list_skills = AsyncMock()
    handled, reply = await try_handle_slash_command(client, "/skill list")
    assert handled is True
    assert "list" in (reply or "")
    assert "已强制加载" in (reply or "")
    client.load_skill.assert_awaited_once_with(skill_name="list")
    client.list_skills.assert_not_called()


@pytest.mark.asyncio
async def test_try_handle_slash_command_dangerously_denied(monkeypatch):
    class _FeishuCfg:
        dangerous_mode_allowed_open_ids = []
        dangerous_mode_allowed_user_ids = []

    class _Cfg:
        feishu = _FeishuCfg()

    monkeypatch.setattr(slash_commands_module, "get_config", lambda: _Cfg())
    client = MagicMock()
    client.feishu_open_id = "ou_denied"
    client.feishu_user_id = "u_denied"
    client.set_dangerous_mode = AsyncMock()

    handled, reply = await try_handle_slash_command(client, "/dangerously on")

    assert handled is True
    assert "Permission denied" in (reply or "")
    client.set_dangerous_mode.assert_not_called()


@pytest.mark.asyncio
async def test_try_handle_slash_command_dangerously_on_off_status(monkeypatch):
    class _FeishuCfg:
        dangerous_mode_allowed_open_ids = ["ou_allowed"]
        dangerous_mode_allowed_user_ids = []

    class _Cfg:
        feishu = _FeishuCfg()

    monkeypatch.setattr(slash_commands_module, "get_config", lambda: _Cfg())
    client = MagicMock()
    client.feishu_open_id = "ou_allowed"
    client.feishu_user_id = "u_1"
    client.set_dangerous_mode = AsyncMock(
        side_effect=[
            {"session_id": "feishu:user:ou_allowed", "dangerous_mode_enabled": True},
            {"session_id": "feishu:user:ou_allowed", "dangerous_mode_enabled": False},
        ]
    )
    client.get_dangerous_mode = AsyncMock(
        return_value={
            "session_id": "feishu:user:ou_allowed",
            "dangerous_mode_enabled": True,
        }
    )

    handled_on, reply_on = await try_handle_slash_command(client, "/dangerously on")
    handled_status, reply_status = await try_handle_slash_command(
        client, "/dangerously status"
    )
    handled_off, reply_off = await try_handle_slash_command(client, "/dangerously off")

    assert handled_on is True
    assert "Dangerous mode is ENABLED" in (reply_on or "")
    assert handled_status is True
    assert "Dangerous mode is ENABLED" in (reply_status or "")
    assert handled_off is True
    assert "Dangerous mode is DISABLED" in (reply_off or "")
    client.set_dangerous_mode.assert_any_await(enabled=True)
    client.set_dangerous_mode.assert_any_await(enabled=False)
    client.get_dangerous_mode.assert_awaited_once()
