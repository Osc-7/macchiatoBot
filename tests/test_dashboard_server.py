from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frontend.dashboard.server import (
    DashboardBackend,
    _format_console_inspect,
    _format_console_ps,
    _format_console_top,
    _format_console_usage,
    create_dashboard_app,
)


class FakeBackend(DashboardBackend):
    async def kernel_snapshot(self):
        return {
            "connected": True,
            "top": {"active_cores": 2},
            "queue": {"queue_size": 1, "active_task_count": 1},
            "cores": [{"session_id": "cli:root", "status": "running", "user_id": "root"}],
            "users": {"frontend": "cli", "users": ["root"]},
            "sessions": ["dashboard:root", "cli:root"],
            "active_session_id": "dashboard:root",
            "models": [
                {"name": "default", "model": "gpt-4o-mini", "active": True},
                {"name": "backup", "model": "kimi-k2.5", "active": False},
            ],
            "token_usage": {"total_tokens": 123},
            "turn_count": 8,
            "dangerous_mode": {"enabled": False},
        }

    async def kernel_kill(self, session_id: str) -> None:
        assert session_id

    async def kernel_cancel(self, session_id: str) -> bool:
        assert session_id
        return True

    async def kernel_spawn(self, session_id: str):
        assert session_id
        return {"session_id": session_id, "status": "running"}

    async def switch_session(self, session_id: str):
        assert session_id
        return {"active_session_id": session_id, "created": False}

    async def clear_context(self) -> None:
        return

    async def switch_model(self, name: str):
        assert name
        return {"active_session_id": "dashboard:root", "info": {"active_provider": name}}

    async def chat(self, text: str, *, session_id: str | None = None):
        assert text
        return {
            "output_text": f"echo: {text}",
            "attachments": [],
            "metadata": {},
            "session_id": session_id or "dashboard:root",
            "token_usage": {"total_tokens": 999},
            "turn_count": 9,
        }

    async def chat_stream(self, text: str, *, session_id: str | None = None):
        yield {"type": "assistant_delta", "delta": "Hel"}
        yield {"type": "assistant_delta", "delta": "lo"}
        yield {"type": "reasoning_delta", "delta": "thinking…"}
        yield {
            "type": "trace",
            "data": {
                "type": "tool_call",
                "tool_call_id": "t1",
                "name": "fake_tool",
                "arguments": {"q": text},
            },
        }
        yield {
            "type": "trace",
            "data": {
                "type": "tool_result",
                "tool_call_id": "t1",
                "name": "fake_tool",
                "success": True,
                "message": "ok",
                "duration_ms": 12,
                "data_preview": "data",
            },
        }
        yield {
            "type": "final",
            "ok": True,
            "output_text": "Hello",
            "metadata": {},
            "attachments": [],
            "session_id": session_id or "dashboard:root",
            "token_usage": {"total_tokens": 5},
            "turn_count": 1,
        }

    async def ping_daemon(self):
        return {"connected": True}

    async def kernel_exec(self, command: str, *, session_id: str | None = None, username: str = ""):
        cmd = (command or "").strip()
        if cmd == "help":
            return {"ok": True, "kind": "text", "output": "Available commands: …"}
        if cmd == "ps":
            data = [{"session_id": "cli:root", "source": "cli", "user_id": "root", "mode": "full", "lifecycle": "running", "idle_seconds": 0, "total_tokens": 0, "turn_count": 0}]
            from frontend.dashboard.server import _format_console_ps

            return {
                "ok": True,
                "kind": "text",
                "output": _format_console_ps(data),
                "data": data,
            }
        if cmd.startswith("/help"):
            return {"ok": True, "kind": "slash", "output": "slash help: ..."}
        if cmd == "kill ":
            return {"ok": False, "kind": "error", "output": "usage: kill <session_id>"}
        return {"ok": False, "kind": "error", "output": f"unknown: {cmd}"}

    async def resolve_permission(self, payload):
        assert payload.permission_id
        return {"ok": True}

    async def resolve_ask_user(self, payload):
        assert payload.batch_id
        return {"ok": True}


class BrokenKernelBackend(DashboardBackend):
    async def kernel_snapshot(self):
        raise RuntimeError("daemon offline")


def _write_yaml(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_dashboard_config_roundtrip(tmp_path: Path) -> None:
    cfg = tmp_path / "config" / "config.yaml"
    _write_yaml(cfg, "llm:\n  active: default\n")
    app = create_dashboard_app(backend=DashboardBackend(config_path=cfg))
    client = TestClient(app)

    resp = client.get("/console/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"]["llm"]["active"] == "default"
    assert "yaml_text" in body

    put = client.put("/console/api/config", json={"yaml_text": "llm:\n  active: foo\n"})
    assert put.status_code == 200

    resp_after = client.get("/console/api/config")
    assert resp_after.status_code == 200
    assert resp_after.json()["content"]["llm"]["active"] == "foo"

    backups = client.get("/console/api/config/backups")
    assert backups.status_code == 200
    assert len(backups.json()["items"]) >= 1

    create_backup = client.post("/console/api/config/backups", json={"reason": "manual"})
    assert create_backup.status_code == 200
    backup_name = create_backup.json()["backup"]["name"]
    assert backup_name.endswith(".yaml")

    restore = client.post("/console/api/config/restore", json={"backup_name": backup_name})
    assert restore.status_code == 200


def test_dashboard_kernel_snapshot() -> None:
    app = create_dashboard_app(backend=FakeBackend())
    client = TestClient(app)
    resp = client.get("/console/api/kernel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert body["top"]["active_cores"] == 2
    assert len(body["cores"]) == 1
    assert body["active_session_id"] == "dashboard:root"
    assert body["turn_count"] == 8
    assert len(body["models"]) == 2


def test_dashboard_kernel_error_fallback() -> None:
    app = create_dashboard_app(backend=BrokenKernelBackend())
    client = TestClient(app)
    resp = client.get("/console/api/kernel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is False
    assert "daemon offline" in body["error"]
    assert body["active_session_id"] == ""
    assert body["turn_count"] == 0


def test_dashboard_kernel_actions() -> None:
    app = create_dashboard_app(backend=FakeBackend())
    client = TestClient(app)

    resp_spawn = client.post("/console/api/kernel/spawn", json={"session_id": "cli:new"})
    assert resp_spawn.status_code == 200
    assert resp_spawn.json()["ok"] is True

    resp_cancel = client.post("/console/api/kernel/cancel", json={"session_id": "cli:new"})
    assert resp_cancel.status_code == 200
    assert resp_cancel.json()["cancelled"] is True

    resp_kill = client.post("/console/api/kernel/kill", json={"session_id": "cli:new"})
    assert resp_kill.status_code == 200
    assert resp_kill.json()["ok"] is True

    resp_switch_session = client.post(
        "/console/api/kernel/session/switch",
        json={"session_id": "cli:new"},
    )
    assert resp_switch_session.status_code == 200
    assert resp_switch_session.json()["active_session_id"] == "cli:new"

    resp_clear = client.post("/console/api/kernel/context/clear", json={})
    assert resp_clear.status_code == 200
    assert resp_clear.json()["ok"] is True

    resp_switch_model = client.post(
        "/console/api/kernel/model/switch",
        json={"name": "backup"},
    )
    assert resp_switch_model.status_code == 200
    assert resp_switch_model.json()["info"]["active_provider"] == "backup"


def test_dashboard_chat_endpoint() -> None:
    app = create_dashboard_app(backend=FakeBackend())
    client = TestClient(app)

    bad = client.post("/console/api/chat", json={"text": "   "})
    assert bad.status_code == 400

    resp = client.post(
        "/console/api/chat",
        json={"text": "hello", "session_id": "dashboard:root"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["output_text"] == "echo: hello"
    assert body["session_id"] == "dashboard:root"


def test_dashboard_health_endpoint() -> None:
    app = create_dashboard_app(backend=FakeBackend())
    client = TestClient(app)
    resp = client.get("/console/api/health")
    assert resp.status_code == 200
    assert resp.json()["connected"] is True


def test_dashboard_console_formatters() -> None:
    ps = _format_console_ps(
        [
            {
                "session_id": "cli:root",
                "source": "cli",
                "user_id": "root",
                "mode": "full",
                "lifecycle": "running",
                "idle_seconds": 3,
                "total_tokens": 42,
                "turn_count": 2,
            }
        ]
    )
    assert "cli:root" in ps
    assert "1 core(s)" in ps

    top = _format_console_top(
        {
            "active_cores": 1,
            "max_cores": 4,
            "queue_depth": 0,
            "inflight_tasks": 0,
            "zombie_cores": 0,
            "uptime_seconds": 125.0,
        }
    )
    assert "active cores" in top
    assert "125s" in top

    usage = _format_console_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "call_count": 1,
            "cost_yuan": 0.0123,
        }
    )
    assert "total tokens" in usage
    assert "0.0123" in usage

    inspect = _format_console_inspect(
        {
            "session_id": "cli:root",
            "source": "cli",
            "user_id": "root",
            "lifecycle": "running",
            "uptime_seconds": 125.0,
            "turn_count": 3,
            "token_usage": {"total_tokens": 100, "prompt_tokens": 80},
            "log_file": "/tmp/session.jsonl",
        }
    )
    assert "Session: cli:root" in inspect
    assert "Token usage:" in inspect
    assert "total_tokens" in inspect
    assert "125s" in inspect


def test_dashboard_kernel_exec() -> None:
    app = create_dashboard_app(backend=FakeBackend())
    client = TestClient(app)

    resp = client.post("/console/api/kernel/exec", json={"command": "help"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["kind"] == "text"
    assert "Available" in body["output"]

    resp_ps = client.post("/console/api/kernel/exec", json={"command": "ps"})
    assert resp_ps.status_code == 200
    ps_body = resp_ps.json()
    assert ps_body["kind"] == "text"
    assert "SESSION" in ps_body["output"]
    assert "cli:root" in ps_body["output"]

    resp_slash = client.post("/console/api/kernel/exec", json={"command": "/help"})
    assert resp_slash.status_code == 200
    assert resp_slash.json()["kind"] == "slash"

    resp_bad = client.post("/console/api/kernel/exec", json={"command": "nope"})
    assert resp_bad.status_code == 200
    assert resp_bad.json()["ok"] is False


def test_dashboard_permission_resolve() -> None:
    app = create_dashboard_app(backend=FakeBackend())
    client = TestClient(app)

    bad = client.post("/console/api/permission/resolve", json={"permission_id": "  "})
    assert bad.status_code == 400

    ok = client.post(
        "/console/api/permission/resolve",
        json={
            "permission_id": "perm-1",
            "allowed": True,
            "persist_acl": False,
        },
    )
    assert ok.status_code == 200
    assert ok.json()["ok"] is True


def test_dashboard_ask_user_resolve() -> None:
    app = create_dashboard_app(backend=FakeBackend())
    client = TestClient(app)

    bad = client.post("/console/api/ask-user/resolve", json={"batch_id": "", "answers": []})
    assert bad.status_code == 400

    ok = client.post(
        "/console/api/ask-user/resolve",
        json={
            "batch_id": "batch-1",
            "answers": [{"question_id": "q1", "selected_option": "a"}],
        },
    )
    assert ok.status_code == 200
    assert ok.json()["ok"] is True


def test_dashboard_serves_index() -> None:
    app = create_dashboard_app(backend=FakeBackend())
    client = TestClient(app)
    resp = client.get("/console/")
    assert resp.status_code == 200
    assert "macchiato" in resp.text.lower()


class SlashBackend(DashboardBackend):
    async def chat(self, text: str, *, session_id=None):
        return {
            "output_text": "(should not be called)",
            "attachments": [],
            "metadata": {},
            "session_id": "dashboard:root",
            "token_usage": {},
            "turn_count": 0,
        }

    async def chat_stream(self, text: str, *, session_id=None):
        assert text.startswith("/")
        yield {"type": "system", "message": "help: ..."}
        yield {
            "type": "final",
            "ok": True,
            "output_text": "help: ...",
            "metadata": {"slash": True},
            "attachments": [],
            "session_id": session_id or "dashboard:root",
            "token_usage": {},
            "turn_count": 0,
        }


def test_dashboard_chat_stream_slash() -> None:
    import json as _json

    app = create_dashboard_app(backend=SlashBackend())
    client = TestClient(app)
    with client.stream(
        "POST",
        "/console/api/chat/stream",
        json={"text": "/help"},
    ) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes()).decode("utf-8")

    events = [_json.loads(line) for line in body.strip().splitlines() if line.strip()]
    assert events[0]["type"] == "system"
    assert events[-1]["type"] == "final"
    assert events[-1]["metadata"]["slash"] is True


def test_dashboard_chat_stream() -> None:
    import json as _json

    app = create_dashboard_app(backend=FakeBackend())
    client = TestClient(app)

    bad = client.post("/console/api/chat/stream", json={"text": " "})
    assert bad.status_code == 400

    with client.stream(
        "POST",
        "/console/api/chat/stream",
        json={"text": "hi", "session_id": "dashboard:root"},
    ) as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_bytes()).decode("utf-8")

    events = [_json.loads(line) for line in body.strip().splitlines() if line.strip()]
    types = [e["type"] for e in events]
    assert types[0] == "assistant_delta"
    assert "reasoning_delta" in types
    assert "trace" in types
    assert events[-1]["type"] == "final"
    tool_traces = [e["data"] for e in events if e["type"] == "trace"]
    assert any(t["type"] == "tool_call" for t in tool_traces)
    assert any(t["type"] == "tool_result" and t["success"] for t in tool_traces)


def test_dashboard_stream_recoveries_filter_by_user_session() -> None:
    from frontend.dashboard.auth import DashboardAuth, DashboardAuthConfig, DashboardUser

    class RecoveryBackend(DashboardBackend):
        async def _with_client(self, username: str = "", timeout_seconds: float | None = None):
            class FakeClient:
                async def poll_stream_recoveries(self):
                    return [
                        {
                            "session_id": "web:alice",
                            "output_text": "alice secret",
                            "metadata": {},
                            "attachments": [],
                        },
                        {
                            "session_id": "web:bob",
                            "output_text": "bob secret",
                            "metadata": {},
                            "attachments": [],
                        },
                    ]

                async def close(self):
                    return None

            return FakeClient()

    auth = DashboardAuth(
        DashboardAuthConfig(
            enabled=True,
            users=(
                DashboardUser(username="alice", password="pass"),
                DashboardUser(username="bob", password="pass"),
            ),
            auth_token="",
            secret="test-dashboard-auth-secret",
            session_ttl_seconds=3600,
            secure_cookies=False,
        )
    )
    app = create_dashboard_app(backend=RecoveryBackend(), auth=auth)
    client = TestClient(app)

    alice_login = client.post(
        "/console/api/auth/login", json={"username": "alice", "password": "pass"}
    )
    assert alice_login.status_code == 200
    alice_cookie = alice_login.cookies

    bob_login = client.post(
        "/console/api/auth/login", json={"username": "bob", "password": "pass"}
    )
    assert bob_login.status_code == 200
    bob_cookie = bob_login.cookies

    alice_resp = client.post(
        "/console/api/chat/recoveries", cookies=alice_cookie
    )
    assert alice_resp.status_code == 200
    alice_data = alice_resp.json()
    assert len(alice_data["recoveries"]) == 1
    assert alice_data["recoveries"][0]["session_id"] == "web:alice"

    bob_resp = client.post(
        "/console/api/chat/recoveries", cookies=bob_cookie
    )
    assert bob_resp.status_code == 200
    bob_data = bob_resp.json()
    assert len(bob_data["recoveries"]) == 1
    assert bob_data["recoveries"][0]["session_id"] == "web:bob"
