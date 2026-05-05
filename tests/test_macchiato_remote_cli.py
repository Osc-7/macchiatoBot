"""macchiato-remote CLI smoke tests."""

from __future__ import annotations

import pytest

from macchiato_remote.cli import main


def test_gen_token_prints_token_and_hints(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gen-token", "--bytes", "16"]) == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines, "expected at least one non-comment line (the token)"
    token_line = lines[0].strip()
    assert len(token_line) >= 12
    assert "MACCHIATO_REMOTE_TOKEN" in out


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


def test_default_ssh_remote_port_for_local_tunnel_server() -> None:
    from macchiato_remote.cli import _default_ssh_remote_port

    assert _default_ssh_remote_port("http://127.0.0.1:19380") == 9380
    assert _default_ssh_remote_port("http://localhost:19380") == 9380
    assert _default_ssh_remote_port("http://110.40.171.96:9380") == 9380
    assert _default_ssh_remote_port("http://110.40.171.96:12345") == 12345
