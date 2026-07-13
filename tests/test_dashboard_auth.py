from __future__ import annotations

from pathlib import Path

from frontend.dashboard.auth import (
    DashboardAuth,
    DashboardAuthConfig,
    DashboardUser,
)
from frontend.dashboard.server import create_dashboard_app
from tests.test_dashboard_server import FakeBackend


def _auth_config(
    *,
    users: tuple[DashboardUser, ...] | None = None,
    auth_token: str = "",
    enabled: bool = True,
) -> DashboardAuthConfig:
    if users is None:
        users = (DashboardUser(username="admin", password="secret"),)
    return DashboardAuthConfig(
        enabled=enabled,
        users=users,
        auth_token=auth_token,
        secret="test-dashboard-auth-secret",
        session_ttl_seconds=3600,
        secure_cookies=False,
    )


def _client(**auth_kwargs):
    from fastapi.testclient import TestClient

    auth = DashboardAuth(_auth_config(**auth_kwargs))
    app = create_dashboard_app(backend=FakeBackend(), auth=auth)
    return TestClient(app)


def test_auth_disabled_allows_anonymous_access() -> None:
    from fastapi.testclient import TestClient

    config = DashboardAuthConfig(
        enabled=False,
        users=(),
        auth_token="",
        secret="unused",
        session_ttl_seconds=3600,
        secure_cookies=False,
    )
    app = create_dashboard_app(backend=FakeBackend(), auth=DashboardAuth(config))
    client = TestClient(app)

    status = client.get("/console/api/auth/status")
    assert status.status_code == 200
    body = status.json()
    assert body["auth_required"] is False
    assert body["authenticated"] is True

    resp = client.get("/console/api/kernel")
    assert resp.status_code == 200


def test_auth_blocks_api_without_credentials() -> None:
    client = _client()
    resp = client.get("/console/api/kernel")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Unauthorized"


def test_auth_redirects_index_to_login() -> None:
    client = _client()
    resp = client.get("/console/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login?next=")


def test_auth_login_and_session_cookie() -> None:
    client = _client()
    bad = client.post("/console/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert bad.status_code == 401

    ok = client.post("/console/api/auth/login", json={"username": "admin", "password": "secret"})
    assert ok.status_code == 200
    assert ok.json()["authenticated"] is True
    assert ok.cookies.get("macchiato_dashboard_session")

    kernel = client.get("/console/api/kernel")
    assert kernel.status_code == 200
    assert kernel.json()["connected"] is True


def test_auth_whitelist_rejects_unknown_user() -> None:
    client = _client(users=(DashboardUser("alice", "secret"),))
    resp = client.post("/console/api/auth/login", json={"username": "bob", "password": "secret"})
    assert resp.status_code == 401


def test_auth_bearer_token() -> None:
    client = _client(users=(), auth_token="api-token")
    resp = client.get("/console/api/kernel", headers={"Authorization": "Bearer api-token"})
    assert resp.status_code == 200


def test_auth_logout_clears_session() -> None:
    client = _client()
    client.post("/console/api/auth/login", json={"username": "admin", "password": "secret"})
    logout = client.post("/console/api/auth/logout")
    assert logout.status_code == 200

    blocked = client.get("/console/api/kernel")
    assert blocked.status_code == 401


def test_auth_login_page_accessible() -> None:
    client = _client()
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "macchiato" in resp.text.lower()


def test_auth_loads_from_yaml_file(tmp_path: Path) -> None:
    auth_yaml = tmp_path / "dashboard_auth.yaml"
    auth_yaml.write_text(
        """
enabled: true
session_secret: yaml-secret
users:
  - username: ops
    password: yaml-pass
""".strip(),
        encoding="utf-8",
    )
    config = DashboardAuthConfig.from_yaml(auth_yaml.read_text(encoding="utf-8"))
    assert config.enabled is True
    assert config.secret == "yaml-secret"
    assert config.users == (DashboardUser("ops", "yaml-pass"),)

    auth = DashboardAuth(config)
    app = create_dashboard_app(backend=FakeBackend(), auth=auth)
    from fastapi.testclient import TestClient

    client = TestClient(app)
    ok = client.post("/console/api/auth/login", json={"username": "ops", "password": "yaml-pass"})
    assert ok.status_code == 200


def _login(client, username: str, password: str) -> None:
    resp = client.post(
        "/console/api/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200


def test_non_admin_kernel_exec_denies_foreign_session() -> None:
    users = (
        DashboardUser(username="admin", password="adminpass"),
        DashboardUser(username="alice", password="alicepass"),
    )
    client = _client(users=users)
    _login(client, "alice", "alicepass")

    denied = client.post(
        "/console/api/kernel/exec",
        json={"session_id": "web:admin", "command": "/clear"},
    )
    assert denied.status_code == 403

    allowed = client.post(
        "/console/api/kernel/exec",
        json={"session_id": "web:alice", "command": "help"},
    )
    assert allowed.status_code == 200


def test_non_admin_chat_denies_foreign_session() -> None:
    users = (
        DashboardUser(username="admin", password="adminpass"),
        DashboardUser(username="alice", password="alicepass"),
    )
    client = _client(users=users)
    _login(client, "alice", "alicepass")

    denied = client.post(
        "/console/api/chat",
        json={"session_id": "web:admin", "text": "hello"},
    )
    assert denied.status_code == 403
