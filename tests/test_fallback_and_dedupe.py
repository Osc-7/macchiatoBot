"""水源公开兜底、错误去重键，以及 P2P 兜底摘录。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_core.llm.error_info import LLMErrorInfo
from frontend.shuiyuan_integration.session import post_public_fallback
from system.kernel.error_notify import (
    _should_send,
    build_error_card,
    build_user_facing_error_card,
    dedupe_key,
    notify_operator_error,
    reset_dedupe,
    set_operator_error_sender,
)
from system.kernel.scheduler import _p2p_fallback_excerpt


def test_feishu_error_cards_explain_cause() -> None:
    err = LLMErrorInfo(
        kind="rate_limit",
        http_status=429,
        provider="kimi_k31_pro",
        model="k3.1-pro",
        message="Client error '429 Too Many Requests'",
    )
    user_body = build_user_facing_error_card(err)["body"]["elements"][0]["content"]
    owner_body = build_error_card(
        error=err, source="feishu", session_id="feishu:user:x"
    )["body"]["elements"][0]["content"]
    for body in (user_body, owner_body):
        assert "兜底" not in body
        assert "owner" not in body
        assert "短时并发/限流" in body
        assert "HTTP 429" in body
        assert "kimi_k31_pro" in body


def test_error_notify_does_not_import_a_frontend_client() -> None:
    import inspect

    import system.kernel.error_notify as mod

    source = inspect.getsource(mod)
    assert "FeishuClient" not in source
    assert "frontend.feishu" not in source


@pytest.mark.asyncio
async def test_operator_notify_waits_for_a_registered_sender() -> None:
    reset_dedupe()
    set_operator_error_sender(None)
    cfg = SimpleNamespace(
        error_reporting=SimpleNamespace(
            enabled=True, notify_chat_id="oc_owner", dedupe_seconds=300
        )
    )
    err = LLMErrorInfo(kind="rate_limit", provider="kimi_k31_pro")
    skipped = await notify_operator_error(
        error=err, source="feishu", session_id="feishu:user:x", config=cfg
    )
    assert skipped is False
    sent: dict[str, str] = {}

    async def _send(**kwargs):  # type: ignore[no-untyped-def]
        sent["chat_id"] = kwargs["chat_id"]

    set_operator_error_sender(_send)
    try:
        delivered = await notify_operator_error(
            error=err, source="feishu", session_id="feishu:user:x", config=cfg
        )
        assert delivered is True
        assert sent["chat_id"] == "oc_owner"
    finally:
        set_operator_error_sender(None)
        reset_dedupe()


def test_dedupe_key_ignores_session() -> None:
    reset_dedupe()
    err = LLMErrorInfo(kind="timeout", provider="deepseek")
    key_a = dedupe_key("shuiyuan", err)
    key_b = dedupe_key("shuiyuan", LLMErrorInfo(kind="timeout", provider="deepseek"))
    assert key_a == "shuiyuan|timeout|deepseek"
    assert key_a == key_b
    first, _ = _should_send(key_a, 300)
    second, _ = _should_send(key_b, 300)
    assert first is True
    assert second is False


def test_p2p_fallback_excerpt_keeps_tail() -> None:
    short = _p2p_fallback_excerpt("  就这句  ")
    assert short == "就这句"
    long = "头" * 2000 + "结尾标记"
    excerpt = _p2p_fallback_excerpt(long)
    assert excerpt.startswith("…（本轮输出已截断，仅保留末尾）")
    assert excerpt.endswith("结尾标记")
    assert len(excerpt) < len(long)


@pytest.mark.asyncio
async def test_public_fallback_uses_structured_error_info(monkeypatch) -> None:
    sent: dict[str, str] = {}

    def fake_post_reply(username, topic_id, text, reply_to, db=None, client=None):
        sent["text"] = text
        return True, "ok", 1

    monkeypatch.setattr(
        "frontend.shuiyuan_integration.reply.post_reply",
        fake_post_reply,
    )
    cfg = SimpleNamespace(error_reporting=SimpleNamespace(public_fallback_enabled=True))
    text = await post_public_fallback(
        username="u",
        topic_id=1,
        reply_to_post_number=2,
        db=None,
        client=object(),
        exc=None,
        error_info={"kind": "context_overflow"},
        config=cfg,
    )
    assert text == "这条太长了，我抓不住重点，拆小一点再问我一次？"
    assert sent["text"] == text
