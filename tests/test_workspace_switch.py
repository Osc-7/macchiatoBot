"""switch_workspace 的 Kernel 准入、默认路径，以及动态运行环境提示。"""

from __future__ import annotations

import platform
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_core.remote.worker_registry import (
    RemoteWorkerConnection,
    get_remote_worker_registry,
    reset_remote_worker_registry_for_tests,
)
from agent_core.remote.workspace_state import (
    activate_remote_workspace,
    clear_remote_workspace_state,
    get_remote_workspace_state,
)
from agent_core.remote.workspace_notice import (
    format_frontend_daemon_switch_message,
    format_frontend_remote_switch_message,
    notify_workspace_switch_frontend,
    workspace_switch_notify_scope,
)
from agent_core.kernel_interface.profile import CoreProfile, may_switch_workspace
from agent_core.remote.workspace_switch import (
    admit_workspace_request,
    format_runtime_environment_section,
    kernel_admit_workspace_request,
    perform_workspace_switch,
)
from macchiato_remote.client import worker_hello_payload


@pytest.fixture(autouse=True)
def _clean_remote_state():
    clear_remote_workspace_state()
    reset_remote_worker_registry_for_tests()
    yield
    clear_remote_workspace_state()
    reset_remote_worker_registry_for_tests()


def _login(login: str, *, hello: dict | None = None, connected: bool = True) -> None:
    if not connected:
        return
    conn = RemoteWorkerConnection(
        login=login,
        send_json=AsyncMock(),
        hello_meta=dict(hello or {}),
    )
    get_remote_worker_registry()._connections[login] = conn


def test_static_workspace_prompt_has_no_host_facts():
    text = Path("src/agent_core/prompts/system/tools_kernel.md").read_text(
        encoding="utf-8"
    )
    assert "switch_workspace" in text
    assert "daemon 主机:" not in text


def test_worker_hello_reports_start_directory():
    payload = worker_hello_payload()
    assert payload["default_workspace"] == str(Path.cwd().resolve())
    assert payload["type"] == "worker_hello"
    assert "device_label" in payload


def test_admit_rejects_unknown_login(monkeypatch):
    monkeypatch.setattr(
        "system.automation.remote_worker_server.remote_server_token_map",
        lambda: {"work": "tok"},
    )
    admission = admit_workspace_request(target="missing")
    assert admission.ok is False
    assert admission.error == "NOT_LOGGED_IN"


def test_admit_rejects_registered_but_offline(monkeypatch):
    monkeypatch.setattr(
        "system.automation.remote_worker_server.remote_server_token_map",
        lambda: {"work": "tok"},
    )
    admission = admit_workspace_request(target="work")
    assert admission.ok is False
    assert admission.error == "NOT_CONNECTED"


def test_admit_rejects_bad_profile(monkeypatch):
    monkeypatch.setattr(
        "system.automation.remote_worker_server.remote_server_token_map",
        lambda: {"work": "tok"},
    )
    _login("work", hello={"default_workspace": "/srv/work"})
    admission = admit_workspace_request(target="work", profile="root")
    assert admission.ok is False
    assert admission.error == "INVALID_PROFILE"


def test_admit_defaults_to_worker_start_directory(monkeypatch):
    monkeypatch.setattr(
        "system.automation.remote_worker_server.remote_server_token_map",
        lambda: {"work": "tok"},
    )
    _login(
        "work",
        hello={"default_workspace": "/srv/work", "device_label": "box", "os": "Linux"},
    )
    admission = admit_workspace_request(target="work")
    assert admission.ok is True
    assert admission.path == "/srv/work"
    assert admission.path_source == "worker_start"
    assert admission.profile == "dev"


def test_kernel_admit_returns_tool_error(monkeypatch):
    monkeypatch.setattr(
        "system.automation.remote_worker_server.remote_server_token_map",
        lambda: {},
    )
    denied = kernel_admit_workspace_request({"target": "nope"})
    assert denied is not None
    assert denied.success is False
    assert denied.error == "NOT_LOGGED_IN"


def test_runtime_section_lists_live_workers_and_daemon(monkeypatch):
    monkeypatch.setattr(
        "system.automation.remote_worker_server.remote_server_token_map",
        lambda: {"work": "tok", "laptop": "tok"},
    )
    _login(
        "work",
        hello={
            "default_workspace": "/srv/work",
            "device_label": "build-box",
            "os": "Linux",
        },
    )
    agent = SimpleNamespace(
        _session_id="cli:root",
        _config=None,
        _source="cli",
        _user_id="root",
        _core_profile=None,
    )
    text = format_runtime_environment_section(agent)
    assert "后端: daemon" in text
    assert "daemon 主机:" in text
    hostname = (platform.node() or "").strip() or "unknown"
    assert hostname in text
    assert "login `work`" in text
    assert "/srv/work" in text
    assert "build-box" in text
    assert "login `laptop`" in text
    assert "未连接" in text
    assert "dev" in text


@pytest.mark.asyncio
async def test_perform_switch_opens_default_path(monkeypatch):
    monkeypatch.setattr(
        "system.automation.remote_worker_server.remote_server_token_map",
        lambda: {"work": "tok"},
    )
    monkeypatch.setattr(
        "agent_core.remote.skills_index.refresh_remote_skills_best_effort",
        AsyncMock(),
    )
    _login("work", hello={"default_workspace": "/srv/work", "device_label": "box"})
    registry = get_remote_worker_registry()

    async def _open(**kwargs):
        assert kwargs["requested_path"] == "/srv/work"
        assert kwargs["profile"] == "dev"
        return SimpleNamespace(resolved_path="/srv/work", device_label="box")

    registry.open_workspace = _open  # type: ignore[method-assign]
    result = await perform_workspace_switch(session_id="cli:root", target="work")
    assert result.success is True
    state = get_remote_workspace_state("cli:root")
    assert state is not None
    assert state.login == "work"
    assert state.requested_path == "/srv/work"
    assert result.data["path_source"] == "worker_start"


@pytest.mark.asyncio
async def test_switch_rebuilds_skills_and_remote_mcp_on_new_workspace(monkeypatch):
    """换机器或同机换目录后，技能索引和远程 MCP 都按新工作区重建。"""
    monkeypatch.setattr(
        "system.automation.remote_worker_server.remote_server_token_map",
        lambda: {"work": "tok", "laptop": "tok"},
    )
    refresh = AsyncMock()
    detach = AsyncMock()
    attach = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "agent_core.remote.skills_index.refresh_remote_skills_best_effort",
        refresh,
    )
    monkeypatch.setattr(
        "agent_core.remote.mcp_lifecycle.before_remote_workspace_released",
        detach,
    )
    monkeypatch.setattr(
        "agent_core.remote.mcp_lifecycle.after_remote_workspace_activated",
        attach,
    )
    activate_remote_workspace(
        session_id="cli:root",
        login="laptop",
        requested_path="/Users/me",
        ttl_seconds=None,
    )
    _login("work", hello={"default_workspace": "/srv/work", "device_label": "box"})
    _login("laptop", hello={"default_workspace": "/Users/me"})
    registry = get_remote_worker_registry()
    closed: list[str] = []

    async def _open(**kwargs):
        return SimpleNamespace(
            resolved_path=kwargs["requested_path"], device_label="box"
        )

    async def _close(**kwargs):
        closed.append(kwargs["login"])

    registry.open_workspace = _open  # type: ignore[method-assign]
    registry.close_workspace = _close  # type: ignore[method-assign]
    agent = SimpleNamespace(_session_id="cli:root", _memory_enabled=False, _context=None)

    result = await perform_workspace_switch(
        session_id="cli:root", target="work", agent=agent
    )
    assert result.success is True
    refresh.assert_awaited_once()
    assert refresh.await_args.kwargs["login"] == "work"
    assert refresh.await_args.kwargs["session_id"] == "cli:root"
    detach.assert_awaited_once()
    attach.assert_awaited_once()
    assert closed == ["laptop"]

    activate_remote_workspace(
        session_id="cli:root",
        login="work",
        requested_path="/srv/work",
        resolved_path="/srv/work",
        ttl_seconds=None,
    )
    refresh.reset_mock()
    detach.reset_mock()
    attach.reset_mock()
    closed.clear()

    result = await perform_workspace_switch(
        session_id="cli:root",
        target="work",
        path="/srv/other",
        agent=agent,
    )
    assert result.success is True
    assert get_remote_workspace_state("cli:root").requested_path == "/srv/other"
    refresh.assert_awaited_once()
    detach.assert_awaited_once()
    attach.assert_awaited_once()
    assert closed == []


@pytest.mark.asyncio
async def test_perform_switch_keeps_previous_when_open_fails(monkeypatch):
    monkeypatch.setattr(
        "system.automation.remote_worker_server.remote_server_token_map",
        lambda: {"work": "tok", "laptop": "tok"},
    )
    activate_remote_workspace(
        session_id="cli:root",
        login="laptop",
        requested_path="/Users/me",
        ttl_seconds=None,
    )
    _login("work", hello={"default_workspace": "/srv/work"})
    _login("laptop", hello={"default_workspace": "/Users/me"})
    registry = get_remote_worker_registry()

    async def _open(**kwargs):
        raise RuntimeError("worker rejected path")

    registry.open_workspace = _open  # type: ignore[method-assign]
    result = await perform_workspace_switch(session_id="cli:root", target="work")
    assert result.success is False
    assert result.error == "OPEN_FAILED"
    state = get_remote_workspace_state("cli:root")
    assert state is not None
    assert state.login == "laptop"


@pytest.mark.asyncio
async def test_release_tells_agent_it_is_back_on_daemon(monkeypatch):
    monkeypatch.setattr(
        "system.automation.remote_worker_server.remote_server_token_map",
        lambda: {},
    )
    activate_remote_workspace(
        session_id="cli:root",
        login="work",
        requested_path="/srv/work",
        ttl_seconds=None,
    )
    ctx = SimpleNamespace(
        messages=[], add_user_message=lambda text: ctx.messages.append(text)
    )
    agent = SimpleNamespace(
        _context=ctx,
        _session_id="cli:root",
        _memory_enabled=False,
        _source="cli",
    )
    result = await perform_workspace_switch(
        session_id="cli:root",
        target="daemon",
        agent=agent,
    )
    assert result.success is True
    assert result.data["backend"] == "daemon"
    assert "daemon 主机:" in result.message
    assert get_remote_workspace_state("cli:root") is None
    assert ctx.messages
    assert "已回到 daemon 所在机器" in ctx.messages[0]


@pytest.mark.asyncio
async def test_switch_pushes_frontend_notice(monkeypatch):
    monkeypatch.setattr(
        "system.automation.remote_worker_server.remote_server_token_map",
        lambda: {"work": "tok"},
    )
    monkeypatch.setattr(
        "agent_core.remote.skills_index.refresh_remote_skills_best_effort",
        AsyncMock(),
    )
    _login("work", hello={"default_workspace": "/srv/work", "device_label": "box"})
    registry = get_remote_worker_registry()

    async def _open(**kwargs):
        return SimpleNamespace(resolved_path="/srv/work", device_label="box")

    registry.open_workspace = _open  # type: ignore[method-assign]
    seen: list[str] = []

    async def _forward(message: str) -> None:
        seen.append(message)

    await notify_workspace_switch_frontend("should be dropped")
    assert seen == []

    async with workspace_switch_notify_scope(_forward):
        result = await perform_workspace_switch(session_id="cli:root", target="work")
    assert result.success is True
    state = get_remote_workspace_state("cli:root")
    assert state is not None
    assert seen == [format_frontend_remote_switch_message(state)]
    assert "macchiatoBot 已将工作区切换至 box（work）" in seen[0]
    assert "工作目录 /srv/work" in seen[0]
    assert "权限档位 dev" in seen[0]

    ctx = SimpleNamespace(
        messages=[], add_user_message=lambda text: ctx.messages.append(text)
    )
    agent = SimpleNamespace(
        _context=ctx,
        _session_id="cli:root",
        _memory_enabled=False,
        _source="cli",
    )
    seen.clear()
    async with workspace_switch_notify_scope(_forward):
        released = await perform_workspace_switch(
            session_id="cli:root",
            target="daemon",
            agent=agent,
        )
    assert released.success is True
    assert len(seen) == 1
    assert seen[0] == format_frontend_daemon_switch_message(previous=state)
    assert "macchiatoBot 已将工作区切换回 daemon" in seen[0]
    assert "已离开 box" in seen[0]


def test_shuiyuan_and_cron_cannot_switch_workspace():
    shuiyuan = CoreProfile.for_shuiyuan(dialog_window_id="alice")
    assert may_switch_workspace(shuiyuan, source="shuiyuan", session_id="shuiyuan:alice") is False
    assert shuiyuan.is_tool_allowed("switch_workspace") is False
    cron = CoreProfile.default_background(frontend_id="cli", dialog_window_id="root")
    assert may_switch_workspace(cron, source="cron", session_id="cron:daily") is False
    owner = CoreProfile.default_full(frontend_id="cli", dialog_window_id="root")
    assert may_switch_workspace(owner, source="cli", session_id="cli:root") is True
    assert owner.is_tool_allowed("switch_workspace") is True


def test_runtime_section_hides_switch_targets_from_shuiyuan(monkeypatch):
    monkeypatch.setattr(
        "system.automation.remote_worker_server.remote_server_token_map",
        lambda: {"work": "tok"},
    )
    _login("work", hello={"default_workspace": "/srv/work", "device_label": "box"})
    agent = SimpleNamespace(
        _session_id="shuiyuan:alice",
        _config=None,
        _source="shuiyuan",
        _user_id="alice",
        _core_profile=CoreProfile.for_shuiyuan(dialog_window_id="alice"),
    )
    text = format_runtime_environment_section(agent)
    assert "无权切换工作区" in text
    assert "可切换机器" not in text
    assert "login `work`" not in text


@pytest.mark.asyncio
async def test_perform_switch_rejects_shuiyuan_session(monkeypatch):
    monkeypatch.setattr(
        "system.automation.remote_worker_server.remote_server_token_map",
        lambda: {"work": "tok"},
    )
    _login("work", hello={"default_workspace": "/srv/work"})
    registry = get_remote_worker_registry()
    opened = {"n": 0}

    async def _open(**kwargs):
        opened["n"] += 1
        return SimpleNamespace(resolved_path="/srv/work", device_label="box")

    registry.open_workspace = _open  # type: ignore[method-assign]
    agent = SimpleNamespace(
        _session_id="shuiyuan:alice",
        _source="shuiyuan",
        _core_profile=CoreProfile.for_shuiyuan(dialog_window_id="alice"),
        _context=None,
        _memory_enabled=False,
    )
    result = await perform_workspace_switch(
        session_id="shuiyuan:alice",
        target="work",
        agent=agent,
    )
    assert result.success is False
    assert result.error == "WORKSPACE_SWITCH_FORBIDDEN"
    assert opened["n"] == 0
    assert get_remote_workspace_state("shuiyuan:alice") is None
