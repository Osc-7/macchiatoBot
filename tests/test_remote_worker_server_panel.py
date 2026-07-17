from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from macchiato_remote.tokens import (expected_token_matches,
                                     load_registered_remote_worker_tokens)
from system.automation import remote_worker_server as rws
from system.automation.remote_worker_server import create_remote_worker_app


def test_remote_login_panel_routes_available() -> None:
    rws.clear_remote_login_state_for_tests()
    app = create_remote_worker_app(token="test-token")
    client = TestClient(app)

    root_resp = client.get("/")
    assert root_resp.status_code == 200
    assert "macchiato remote login panel" in root_resp.text

    panel_resp = client.get("/remote/login")
    assert panel_resp.status_code == 200
    assert "Approve a pending remote login request" in panel_resp.text


def test_remote_healthz_available() -> None:
    rws.clear_remote_login_state_for_tests()
    app = create_remote_worker_app(token="test-token")
    client = TestClient(app)

    resp = client.get("/remote/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_remote_worker_ws_max_size_covers_blob_responses() -> None:
    from macchiato_remote.protocol import REMOTE_BLOB_MAX_BYTES, REMOTE_WS_MAX_SIZE
    from uvicorn.config import Config

    assert rws.remote_worker_ws_max_size() == REMOTE_WS_MAX_SIZE
    assert REMOTE_WS_MAX_SIZE > Config("x:app").ws_max_size
    assert REMOTE_WS_MAX_SIZE > REMOTE_BLOB_MAX_BYTES * 4 // 3


def test_remote_device_login_flow_requires_approver_secret(
    monkeypatch,
    tmp_path,
) -> None:
    rws.clear_remote_login_state_for_tests()
    token_file = tmp_path / "remote_worker_tokens.json"
    monkeypatch.setenv("MACCHIATO_REMOTE_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("MACCHIATO_REMOTE_LOGIN_APPROVER_SECRET", raising=False)
    monkeypatch.delenv("MACCHIATO_REMOTE_LOGIN_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setattr(rws, "_remote_login_feishu_enabled", lambda: False)
    app = create_remote_worker_app(token="test-token")
    client = TestClient(app)

    started = client.post(
        "/remote/login/start",
        json={"login": "personal", "device_name": "mbp"},
    ).json()
    assert started["ok"] is False
    assert started["error"] == "login_panel_disabled"


def test_remote_bootstrap_exchange_issue_worker_token(
    monkeypatch,
    tmp_path,
) -> None:
    rws.clear_remote_login_state_for_tests()
    token_file = tmp_path / "remote_worker_tokens.json"
    monkeypatch.setenv("MACCHIATO_REMOTE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("MACCHIATO_REMOTE_LOGIN_BOOTSTRAP_TOKEN", "boot-abc")
    monkeypatch.delenv("MACCHIATO_REMOTE_LOGIN_APPROVER_SECRET", raising=False)
    monkeypatch.setattr(rws, "_remote_login_feishu_enabled", lambda: False)
    app = create_remote_worker_app(token="test-token")
    client = TestClient(app)

    missing = client.post(
        "/remote/login/start",
        json={"login": "personal", "device_name": "mbp"},
    ).json()
    assert missing["ok"] is False
    assert missing["error"] == "bootstrap_token_required"

    bad = client.post(
        "/remote/login/start",
        json={"login": "personal", "device_name": "mbp", "bootstrap_token": "wrong"},
    ).json()
    assert bad["ok"] is False
    assert bad["error"] == "forbidden"

    ok = client.post(
        "/remote/login/start",
        json={"login": "personal", "device_name": "mbp", "bootstrap_token": "boot-abc"},
    ).json()
    assert ok["ok"] is True
    assert ok["status"] == "approved"
    assert ok["mode"] == "bootstrap_exchange"
    assert ok["token"]

    registered = load_registered_remote_worker_tokens(token_file)
    assert "personal" in registered
    assert expected_token_matches(ok["token"], registered["personal"])


def test_remote_device_login_flow_approve_and_poll(
    monkeypatch,
    tmp_path,
) -> None:
    rws.clear_remote_login_state_for_tests()
    token_file = tmp_path / "remote_worker_tokens.json"
    monkeypatch.setenv("MACCHIATO_REMOTE_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("MACCHIATO_REMOTE_LOGIN_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setenv("MACCHIATO_REMOTE_LOGIN_APPROVER_SECRET", "secret-123")
    monkeypatch.setattr(rws, "_remote_login_feishu_enabled", lambda: False)
    app = create_remote_worker_app(token="test-token")
    client = TestClient(app)

    started = client.post(
        "/remote/login/start",
        json={"login": "personal", "device_name": "mbp"},
    ).json()
    assert started["ok"] is True
    assert started["device_code"]
    assert started["user_code"]

    pending = client.post(
        "/remote/login/poll",
        json={"device_code": started["device_code"]},
    ).json()
    assert pending["ok"] is False
    assert pending["status"] == "authorization_pending"

    denied = client.post(
        "/remote/login/approve",
        json={
            "user_code": started["user_code"],
            "approver_secret": "wrong-secret",
            "approve": True,
        },
    ).json()
    assert denied["ok"] is False
    assert denied["error"] == "forbidden"

    approved = client.post(
        "/remote/login/approve",
        json={
            "user_code": started["user_code"],
            "approver_secret": "secret-123",
            "approve": True,
        },
    ).json()
    assert approved["ok"] is True
    assert approved["status"] == "approved"

    final = client.post(
        "/remote/login/poll",
        json={"device_code": started["device_code"]},
    ).json()
    assert final["ok"] is True
    assert final["status"] == "approved"
    assert final["login"] == "personal"
    assert final["token"]

    registered = load_registered_remote_worker_tokens(token_file)
    assert "personal" in registered
    assert expected_token_matches(final["token"], registered["personal"])


def test_remote_device_login_start_feishu_card_mode(
    monkeypatch,
    tmp_path,
) -> None:
    rws.clear_remote_login_state_for_tests()
    token_file = tmp_path / "remote_worker_tokens.json"
    monkeypatch.setenv("MACCHIATO_REMOTE_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("MACCHIATO_REMOTE_LOGIN_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.delenv("MACCHIATO_REMOTE_LOGIN_APPROVER_SECRET", raising=False)
    monkeypatch.setattr(rws, "_remote_login_feishu_enabled", lambda: True)

    async def _fake_send(record):
        assert record.get("login") == "personal"
        return True, ""

    monkeypatch.setattr(rws, "_send_remote_login_approval_card", _fake_send)
    app = create_remote_worker_app(token="test-token")
    client = TestClient(app)
    started = client.post(
        "/remote/login/start",
        json={"login": "personal", "device_name": "mbp"},
    ).json()
    assert started["ok"] is True
    assert started["mode"] == "feishu_card"
    assert started["status"] == "authorization_pending"
    assert started["device_code"]


def test_resolve_remote_login_request_from_feishu_requires_allowlist(
    monkeypatch,
    tmp_path,
) -> None:
    rws.clear_remote_login_state_for_tests()
    token_file = tmp_path / "remote_worker_tokens.json"
    monkeypatch.setenv("MACCHIATO_REMOTE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("MACCHIATO_REMOTE_LOGIN_APPROVER_OPEN_IDS", "ou_admin")
    monkeypatch.delenv("MACCHIATO_REMOTE_LOGIN_BOOTSTRAP_TOKEN", raising=False)
    monkeypatch.setenv("MACCHIATO_REMOTE_LOGIN_APPROVER_SECRET", "secret-123")
    monkeypatch.setattr(rws, "_remote_login_feishu_enabled", lambda: False)
    app = create_remote_worker_app(token="test-token")
    client = TestClient(app)
    started = client.post(
        "/remote/login/start",
        json={"login": "personal", "device_name": "mbp"},
    ).json()
    assert started["ok"] is True
    request_id = str(started["device_code"])

    kind, msg, _card = rws.resolve_remote_login_request_from_feishu(
        request_id=request_id,
        approve=True,
        approver_open_id="ou_other",
        approver_user_id="",
    )
    assert kind == "error"
    assert "没有远程登录审批权限" in msg

    kind2, msg2, _card2 = rws.resolve_remote_login_request_from_feishu(
        request_id=request_id,
        approve=True,
        approver_open_id="ou_admin",
        approver_user_id="",
    )
    assert kind2 == "success"
    assert "已批准" in msg2


def test_remote_login_approver_allowlist_fallback_to_config(monkeypatch) -> None:
    monkeypatch.delenv("MACCHIATO_REMOTE_LOGIN_APPROVER_OPEN_IDS", raising=False)
    monkeypatch.delenv("MACCHIATO_REMOTE_LOGIN_APPROVER_USER_IDS", raising=False)
    fake_cfg = SimpleNamespace(
        feishu=SimpleNamespace(
            remote_login_approver_open_ids=["ou_cfg_admin"],
            remote_login_approver_user_ids=["u_cfg_admin"],
        )
    )
    monkeypatch.setattr(rws, "get_config", lambda: fake_cfg)

    assert rws.remote_login_allowed_approver_open_ids() == {"ou_cfg_admin"}
    assert rws.remote_login_allowed_approver_user_ids() == {"u_cfg_admin"}
