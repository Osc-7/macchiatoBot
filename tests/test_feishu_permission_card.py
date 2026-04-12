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
    build_permission_request_card,
    interactive_content_string,
    parse_card_action_value,
)

def test_build_permission_card_is_valid_json_roundtrip() -> None:
    card = build_permission_request_card(
        permission_id="550e8400-e29b-41d4-a716-446655440000",
        summary="写 /tmp",
        kind="file_write",
        timeout_seconds=120.0,
    )
    s = interactive_content_string(card)
    data = json.loads(s)
    assert data["schema"] == "2.0"
    assert "column_set" in json.dumps(data["body"]["elements"])


def test_parse_card_action_value() -> None:
    pid, dec, pfx = parse_card_action_value(
        {
            "permission_id": "abc",
            "macchiato_permission": "allow",
            "path_prefix": "/tmp",
        }
    )
    assert pid == "abc"
    assert dec == ALLOW
    assert pfx == "/tmp"


@pytest.mark.asyncio
async def test_card_callback_resolves_wait_registry(monkeypatch) -> None:
    from frontend.feishu import card_callback as cc

    async def _local_ipc(rv):
        return cc.execute_card_permission_resolution(rv)

    monkeypatch.setattr(cc, "resolve_card_via_daemon_ipc", _local_ipc)

    set_permission_notify_hook(lambda *_: None)
    try:
        pid, fut = register_permission_wait()

        body = {
            "header": {"event_type": "card.action.trigger", "token": None},
            "event": {
                "action": {
                    "value": {"permission_id": pid, "macchiato_permission": "allow"},
                }
            },
        }
        resp = await handle_feishu_card_action(body)
        assert resp.status_code == 200
        payload = json.loads(resp.body.decode())
        assert "toast" in payload

        decision = await asyncio.wait_for(fut, timeout=2.0)
        assert decision.allowed is True
    finally:
        set_permission_notify_hook(None)
