from __future__ import annotations

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


def test_remote_device_login_flow_requires_approver_secret(
    monkeypatch,
    tmp_path,
) -> None:
    rws.clear_remote_login_state_for_tests()
    token_file = tmp_path / "remote_worker_tokens.json"
    monkeypatch.setenv("MACCHIATO_REMOTE_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("MACCHIATO_REMOTE_LOGIN_APPROVER_SECRET", raising=False)
    app = create_remote_worker_app(token="test-token")
    client = TestClient(app)

    started = client.post(
        "/remote/login/start",
        json={"login": "personal", "device_name": "mbp"},
    ).json()
    assert started["ok"] is False
    assert started["error"] == "login_panel_disabled"


def test_remote_device_login_flow_approve_and_poll(
    monkeypatch,
    tmp_path,
) -> None:
    rws.clear_remote_login_state_for_tests()
    token_file = tmp_path / "remote_worker_tokens.json"
    monkeypatch.setenv("MACCHIATO_REMOTE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("MACCHIATO_REMOTE_LOGIN_APPROVER_SECRET", "secret-123")
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
