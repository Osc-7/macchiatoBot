"""飞书权限卡片 JSON 与 card.action.trigger 处理。"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_core.permissions.wait_registry import (
    register_permission_wait,
    set_permission_notify_hook,
)
from frontend.feishu.card_callback import handle_feishu_card_action
from frontend.feishu.permission_card import (
    ALLOW,
    CLARIFY,
    USER_INSTRUCTION_FIELD,
    build_permission_request_card,
    interactive_content_string,
    parse_permission_card_callback,
)


def test_build_permission_card_no_path_two_columns_plus_form() -> None:
    card = build_permission_request_card(
        permission_id="550e8400-e29b-41d4-a716-446655440000",
        summary="写 /tmp",
        kind="file_write",
        timeout_seconds=120.0,
    )
    s = interactive_content_string(card)
    data = json.loads(s)
    assert data["schema"] == "2.0"
    elems = data["body"]["elements"]
    assert elems[1]["tag"] == "column_set"
    assert len(elems[1]["columns"]) == 2
    assert elems[2]["tag"] == "form"
    assert any(
        el.get("name") == USER_INSTRUCTION_FIELD
        for el in elems[2]["elements"]
        if isinstance(el, dict) and el.get("tag") == "input"
    )


def test_build_with_path_prefix_three_action_columns() -> None:
    card = build_permission_request_card(
        permission_id="550e8400-e29b-41d4-a716-446655440000",
        summary="写外部",
        path_prefix="/data/x",
    )
    cols = card["body"]["elements"][1]["columns"]
    assert len(cols) == 3
    labels = [cols[i]["elements"][0]["text"]["content"] for i in range(3)]
    assert "Once" in labels
    assert "Always" in labels
    assert "Deny" in labels


def test_build_resolved_clarify_shows_instruction() -> None:
    card = build_permission_request_card(
        permission_id="p1",
        summary="s",
        resolved="clarify",
        resolved_user_instruction="请只写入 /tmp/a",
    )
    md = card["body"]["elements"][0]["content"]
    assert "Clarify" in md
    assert "/tmp/a" in md


def test_merge_form_value_into_instruction() -> None:
    pid, dec, _pfx, _, _, _, ui, _pacl = parse_permission_card_callback(
        {"permission_id": "abc", "macchiato_permission": CLARIFY},
        {USER_INSTRUCTION_FIELD: "  hello  "},
    )
    assert pid == "abc"
    assert dec == CLARIFY
    assert ui == "hello"


def test_parse_allow_without_form() -> None:
    pid, dec, pfx, sum_e, kind_e, te, ui, pacl = parse_permission_card_callback(
        {
            "permission_id": "abc",
            "macchiato_permission": "allow",
            "path_prefix": "/tmp",
            "summary_echo": "摘要",
            "kind_echo": "k",
            "timeout_echo": 60.0,
        },
        None,
    )
    assert pid == "abc"
    assert dec == ALLOW
    assert pfx == "/tmp"
    assert sum_e == "摘要"
    assert kind_e == "k"
    assert te == 60.0
    assert ui == ""
    assert pacl is False


def test_parse_clarify() -> None:
    pid, dec, pfx, *_ = parse_permission_card_callback(
        {"permission_id": "x", "macchiato_permission": CLARIFY},
        None,
    )
    assert pid == "x"
    assert dec == CLARIFY
    assert pfx is None


@pytest.mark.asyncio
async def test_card_callback_resolves_wait_registry(monkeypatch) -> None:
    from frontend.feishu import card_callback as cc

    async def _local_ipc(raw_val, form_value=None):
        return cc.execute_card_permission_resolution(raw_val, form_value)

    monkeypatch.setattr(cc, "resolve_card_via_daemon_ipc", _local_ipc)

    set_permission_notify_hook(lambda *_: None)
    try:
        pid, fut = register_permission_wait()

        body = {
            "header": {"event_type": "card.action.trigger", "token": None},
            "event": {
                "action": {
                    "value": {
                        "permission_id": pid,
                        "macchiato_permission": "allow",
                        "summary_echo": "测试摘要",
                        "kind_echo": "file_write",
                    },
                }
            },
        }
        resp = await handle_feishu_card_action(body)
        assert resp.status_code == 200
        payload = json.loads(resp.body.decode())
        assert "toast" in payload
        assert payload.get("card", {}).get("type") == "raw"
        assert payload["card"]["data"]["body"]["elements"][0]["content"].find("✅") >= 0

        decision = await asyncio.wait_for(fut, timeout=2.0)
        assert decision.allowed is True
        assert decision.persist_acl is False
    finally:
        set_permission_notify_hook(None)


@pytest.mark.asyncio
async def test_card_callback_clarify_with_form_value(monkeypatch) -> None:
    from frontend.feishu import card_callback as cc

    async def _local_ipc(raw_val, form_value=None):
        return cc.execute_card_permission_resolution(raw_val, form_value)

    monkeypatch.setattr(cc, "resolve_card_via_daemon_ipc", _local_ipc)

    set_permission_notify_hook(lambda *_: None)
    try:
        pid, fut = register_permission_wait()

        body = {
            "header": {"event_type": "card.action.trigger", "token": None},
            "event": {
                "action": {
                    "value": {
                        "permission_id": pid,
                        "macchiato_permission": CLARIFY,
                        "summary_echo": "s",
                    },
                    "form_value": {USER_INSTRUCTION_FIELD: "只写 home 目录"},
                }
            },
        }
        resp = await handle_feishu_card_action(body)
        assert resp.status_code == 200
        payload = json.loads(resp.body.decode())
        assert "toast" in payload
        assert payload.get("card", {}).get("type") == "raw"
        assert "home" in payload["card"]["data"]["body"]["elements"][0]["content"]

        decision = await asyncio.wait_for(fut, timeout=2.0)
        assert decision.allowed is False
        assert decision.clarify_requested is True
        assert decision.user_instruction == "只写 home 目录"
    finally:
        set_permission_notify_hook(None)
