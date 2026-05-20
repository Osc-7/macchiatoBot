"""macchiato-remote CLI smoke tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from macchiato_remote.cli import main
from macchiato_remote.tokens import (expected_token_matches,
                                     load_registered_remote_worker_tokens)


def test_gen_token_prints_token_and_hints(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gen-token", "--bytes", "16"]) == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines, "expected at least one non-comment line (the token)"
    token_line = lines[0].strip()
    assert len(token_line) >= 12
    assert "MACCHIATO_REMOTE_TOKEN" in out


def test_gen_token_with_login_prints_multi_machine_hint(
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    token_file = tmp_path / "remote_worker_tokens.json"
    assert (
        main(
            [
                "gen-token",
                "--bytes",
                "16",
                "--login",
                "work-mbp",
                "--token-file",
                str(token_file),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("#")]
    token_line = lines[0].strip()
    assert len(token_line) >= 12
    assert "已注册到服务器 token 文件" in out
    assert str(token_file) in out
    assert "--login work-mbp" in out
    registered = load_registered_remote_worker_tokens(token_file)
    assert set(registered) == {"work-mbp"}
    assert expected_token_matches(token_line, registered["work-mbp"])


def test_gen_token_with_no_register_prints_env_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(["gen-token", "--bytes", "16", "--login", "work-mbp", "--no-register"])
        == 0
    )
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("#")]
    token_line = lines[0].strip()
    assert "MACCHIATO_REMOTE_TOKENS" in out
    assert f"work-mbp={token_line}" in out


def test_remote_server_default_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from system.automation import remote_worker_server as rws

    monkeypatch.delenv("MACCHIATO_REMOTE_PORT", raising=False)
    assert rws.DEFAULT_REMOTE_WORKER_WEBSOCKET_PORT == 9380
    assert rws.remote_server_port() == 9380


def test_remote_server_port_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from system.automation import remote_worker_server as rws

    monkeypatch.setenv("MACCHIATO_REMOTE_PORT", "12345")
    assert rws.remote_server_port() == 12345
    monkeypatch.delenv("MACCHIATO_REMOTE_PORT", raising=False)
    # invalid env falls back to default constant
    monkeypatch.setenv("MACCHIATO_REMOTE_PORT", "not-a-port")
    assert rws.remote_server_port() == rws.DEFAULT_REMOTE_WORKER_WEBSOCKET_PORT


def test_remote_server_token_map_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    from system.automation import remote_worker_server as rws

    reg = tmp_path / "remote_worker_tokens.json"
    reg.write_text('{"version": 1, "tokens": {}}', encoding="utf-8")
    monkeypatch.setenv("MACCHIATO_REMOTE_TOKEN_FILE", str(reg))
    monkeypatch.setenv(
        "MACCHIATO_REMOTE_TOKENS",
        "work-mbp=tok1,home-mini=tok2\nstudio-linux:tok3",
    )
    assert rws.remote_server_token_map() == {
        "work-mbp": "tok1",
        "home-mini": "tok2",
        "studio-linux": "tok3",
    }


def test_remote_server_token_map_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from macchiato_remote.tokens import register_remote_worker_token
    from system.automation import remote_worker_server as rws

    token_file = tmp_path / "remote_worker_tokens.json"
    register_remote_worker_token("work-mbp", "machine-token", token_file)
    monkeypatch.setenv("MACCHIATO_REMOTE_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("MACCHIATO_REMOTE_TOKENS", raising=False)
    tokens = rws.remote_server_token_map()
    assert set(tokens) == {"work-mbp"}
    assert expected_token_matches("machine-token", tokens["work-mbp"])


def test_remote_worker_token_verification_per_login_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from system.automation import remote_worker_server as rws

    monkeypatch.setenv("MACCHIATO_REMOTE_TOKEN", "shared")
    assert rws.verify_remote_worker_token(
        login="unknown", supplied_token="shared"
    ) == (True, "ok")
    assert rws.verify_remote_worker_token(
        login="work-mbp",
        supplied_token="machine",
        token_map={"work-mbp": "machine"},
    ) == (True, "ok")
    assert rws.verify_remote_worker_token(
        login="work-mbp",
        supplied_token="shared",
        token_map={"work-mbp": "machine"},
    ) == (False, "token_mismatch")

    monkeypatch.delenv("MACCHIATO_REMOTE_TOKEN", raising=False)
    assert rws.verify_remote_worker_token(
        login="unknown",
        supplied_token="anything",
        token_map={"work-mbp": "machine"},
    ) == (False, "unknown_login")


def test_remote_worker_websocket_rejects_bad_token() -> None:
    from system.automation import remote_worker_server as rws

    app = rws.create_remote_worker_app(token="good")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/remote/worker/work-mbp?token=bad"):
                pass
    assert exc.value.code == 1008


def test_remote_worker_websocket_accepts_login_specific_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from system.automation import remote_worker_server as rws

    monkeypatch.setenv("MACCHIATO_REMOTE_TOKENS", "work-mbp=machine-token")
    app = rws.create_remote_worker_app()
    with TestClient(app) as client:
        with client.websocket_connect(
            "/remote/worker/work-mbp?token=machine-token"
        ) as websocket:
            websocket.close()


def test_remote_worker_websocket_accepts_registered_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from macchiato_remote.tokens import register_remote_worker_token
    from system.automation import remote_worker_server as rws

    token_file = tmp_path / "remote_worker_tokens.json"
    register_remote_worker_token("work-mbp", "machine-token", token_file)
    monkeypatch.setenv("MACCHIATO_REMOTE_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("MACCHIATO_REMOTE_TOKENS", raising=False)
    monkeypatch.delenv("MACCHIATO_REMOTE_TOKEN", raising=False)

    app = rws.create_remote_worker_app()
    with TestClient(app) as client:
        with client.websocket_connect(
            "/remote/worker/work-mbp?token=machine-token"
        ) as websocket:
            websocket.close()


def test_default_ssh_remote_port_for_local_tunnel_server() -> None:
    from macchiato_remote.cli import _default_ssh_remote_port

    assert _default_ssh_remote_port("http://127.0.0.1:19380") == 9380
    assert _default_ssh_remote_port("http://localhost:19380") == 9380
    assert _default_ssh_remote_port("http://203.0.113.10:9380") == 9380
    assert _default_ssh_remote_port("http://203.0.113.10:12345") == 12345


def test_login_accepts_positional_server_with_static_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from macchiato_remote import cli as rc

    cfg_path = tmp_path / "remote.json"
    monkeypatch.setattr(rc, "CONFIG_PATH", cfg_path)
    assert (
        main(
            [
                "login",
                "203.0.113.10:9380",
                "--login",
                "personal",
                "--token",
                "tok-1",
            ]
        )
        == 0
    )
    saved = cfg_path.read_text(encoding="utf-8")
    assert "http://203.0.113.10:9380" in saved
    assert '"login": "personal"' in saved
    assert '"token": "tok-1"' in saved


def test_login_device_flow_bootstrap_exchange_writes_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from macchiato_remote import cli as rc

    cfg_path = tmp_path / "remote.json"
    monkeypatch.setattr(rc, "CONFIG_PATH", cfg_path)

    calls: list[tuple[str, dict]] = []

    def _fake_post(url: str, payload: dict, *, timeout: float = 10.0) -> tuple[int, dict]:
        _ = timeout
        calls.append((url, payload))
        return 200, {
            "ok": True,
            "status": "approved",
            "login": "personal",
            "token": "issued-worker-token",
        }

    monkeypatch.setattr(rc, "_http_post_json", _fake_post)
    assert (
        main(
            [
                "login",
                "203.0.113.10:9380",
                "--login",
                "personal",
                "--auth-token",
                "boot-abc",
            ]
        )
        == 0
    )
    assert calls
    assert calls[0][0].endswith("/remote/login/start")
    assert calls[0][1]["bootstrap_token"] == "boot-abc"

    saved = cfg_path.read_text(encoding="utf-8")
    assert '"token": "issued-worker-token"' in saved


def test_remote_worker_client_normalizes_server_without_scheme() -> None:
    from macchiato_remote.client import RemoteWorkerClient

    c = RemoteWorkerClient(server="149.28.149.135:9380", login="sii", token="tok")
    ws_url = c._websocket_url()
    assert ws_url.startswith("ws://149.28.149.135:9380/remote/worker/sii")
    assert "token=tok" in ws_url


def test_worker_hello_payload() -> None:
    from macchiato_remote.client import worker_hello_payload
    from macchiato_remote.protocol import (
        REMOTE_PROTOCOL_VERSION,
        REMOTE_WORKER_CAPABILITIES,
    )

    payload = worker_hello_payload()
    assert payload["type"] == "worker_hello"
    assert payload["protocol_version"] == REMOTE_PROTOCOL_VERSION
    assert payload["capabilities"] == list(REMOTE_WORKER_CAPABILITIES)
    assert isinstance(payload["package_version"], str)


def test_cli_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "macchiato-remote" in out
    assert "protocol" in out


def test_remote_worker_websocket_accepts_worker_hello(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from system.automation import remote_worker_server as rws

    monkeypatch.setenv("MACCHIATO_REMOTE_TOKENS", "work-mbp=machine-token")
    app = rws.create_remote_worker_app()
    with TestClient(app) as client:
        with client.websocket_connect(
            "/remote/worker/work-mbp?token=machine-token"
        ) as websocket:
            websocket.send_json(
                {
                    "type": "worker_hello",
                    "protocol_version": 2,
                    "capabilities": ["exec"],
                    "package_version": "0.2.0",
                }
            )
            websocket.close()
