from __future__ import annotations

from typing import Any, Dict

import pytest

import automation_daemon as daemon
from agent_core.config import get_config, reset_config


@pytest.fixture(autouse=True)
def _reset_global_config() -> None:
    """Ensure global Config is reset around each test in this module."""
    reset_config()
    yield
    reset_config()


@pytest.mark.asyncio
async def test_maybe_notify_feishu_activity_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Feishu or automation activity push is disabled, notifier should be a no-op."""

    cfg = get_config()
    cfg_disabled = cfg.model_copy(deep=True)
    cfg_disabled.feishu.enabled = False

    monkeypatch.setattr(daemon, "get_config", lambda: cfg_disabled)

    called: Dict[str, Any] = {}

    async def _noop_send_feishu_agent_reply(**kwargs: Any) -> None:  # noqa: ANN401
        called["would_send"] = True

    monkeypatch.setattr(daemon, "send_feishu_agent_reply", _noop_send_feishu_agent_reply)

    record = {
        "timestamp": "2026-03-06T12:00:00",
        "source": "cron:sync.course",
        "result": {"success": True, "message": "sync ok", "error": None},
    }

    await daemon._maybe_notify_feishu_activity(record)

    assert "would_send" not in called


@pytest.mark.asyncio
async def test_maybe_notify_feishu_activity_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When enabled and chat_id is configured, notifier should send a Feishu message."""

    cfg = get_config()
    cfg_enabled = cfg.model_copy(deep=True)
    cfg_enabled.feishu.enabled = True
    cfg_enabled.feishu.automation_activity_enabled = True
    cfg_enabled.feishu.automation_activity_chat_id = "chat-automation-123"

    monkeypatch.setattr(daemon, "get_config", lambda: cfg_enabled)

    sent: Dict[str, Any] = {}

    async def _capture_send_feishu_agent_reply(**kwargs: Any) -> None:  # noqa: ANN401
        sent["chat_id"] = kwargs.get("chat_id")
        sent["text"] = kwargs.get("output_text")
        sent["header"] = kwargs.get("markdown_card_header_title")

    monkeypatch.setattr(daemon, "send_feishu_agent_reply", _capture_send_feishu_agent_reply)

    record = {
        "timestamp": "2026-03-06T12:00:00",
        "source": "cron:sync.course",
        "result": {
            "success": True,
            "message": "课程同步完成，共创建 3 条事件",
            "error": None,
        },
    }

    await daemon._maybe_notify_feishu_activity(record)

    assert sent.get("chat_id") == "chat-automation-123"
    assert sent.get("header") == "定时任务"
    text = sent.get("text", "")
    assert "cron:sync.course" in text
    assert "课程同步完成" in text
