"""request_permission 工具与 wait_registry。"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_core.config import CommandToolsConfig, Config, LLMConfig
from agent_core.permissions.wait_registry import (
    PermissionDecision,
    resolve_permission,
    set_permission_notify_hook,
)
from agent_core.agent.writable_ephemeral_grants import (
    clear_ephemeral_writable_grants_for_tests,
)
from agent_core.permissions.bash_danger_approvals import (
    clear_bash_danger_grant_for_tests,
    consume_bash_danger_grant,
)
from agent_core.tools.request_permission_tool import RequestPermissionTool

pytestmark = pytest.mark.asyncio


@pytest.fixture
def patched_get_config(monkeypatch, tmp_path):
    c = Config(
        llm=LLMConfig(api_key="k", model="m"),
        command_tools=CommandToolsConfig(acl_base_dir=str(tmp_path / "acl")),
    )
    import agent_core.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "get_config", lambda: c)
    return tmp_path, c


async def test_request_permission_allow_with_prefix_persists_acl(
    patched_get_config, monkeypatch
):
    tmp_path, _c = patched_get_config
    captured: list[str] = []

    def _notify(pid: str, payload: object) -> None:
        captured.append(pid)

    set_permission_notify_hook(_notify)

    tool = RequestPermissionTool()
    exec_ctx = {"source": "cli", "user_id": "alice"}

    async def _run() -> object:
        return await tool.execute(
            summary="need write",
            kind="bash_write",
            timeout_seconds=5.0,
            __execution_context__=exec_ctx,
        )

    task = asyncio.create_task(_run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if captured:
            break
    assert captured, "permission_id should be notified"
    pid = captured[0]
    ok = resolve_permission(
        pid,
        PermissionDecision(
            allowed=True,
            path_prefix=str(tmp_path / "extra"),
            persist_acl=True,
        ),
    )
    assert ok
    result = await task
    assert result.success
    acl_file = tmp_path / "acl" / "cli" / "alice" / "writable_roots.json"
    assert acl_file.is_file()
    data = json.loads(acl_file.read_text(encoding="utf-8"))
    assert str(tmp_path / "extra") in data["prefixes"]


async def test_request_permission_bash_dangerous_registers_grant(
    patched_get_config, monkeypatch
):
    """bash_dangerous_command 批准后登记一次性 bash 执行权。"""
    tmp_path, _c = patched_get_config
    clear_bash_danger_grant_for_tests()
    captured: list[str] = []

    def _notify(pid: str, payload: object) -> None:
        captured.append(pid)

    set_permission_notify_hook(_notify)
    tool = RequestPermissionTool()
    exec_ctx = {"source": "cli", "user_id": "alice"}
    cmd = "rm -rf /tmp/x"

    async def _run():
        return await tool.execute(
            summary="delete",
            kind="bash_dangerous_command",
            details=json.dumps({"command": cmd}),
            timeout_seconds=5.0,
            __execution_context__=exec_ctx,
        )

    task = asyncio.create_task(_run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if captured:
            break
    assert captured, "permission_id should be notified"
    pid = captured[0]
    ok = resolve_permission(
        pid,
        PermissionDecision(allowed=True, persist_acl=False),
    )
    assert ok
    result = await task
    assert result.success
    assert result.data and result.data.get("permission_id") == pid
    assert consume_bash_danger_grant(pid, cmd)
    assert not consume_bash_danger_grant(pid, cmd)
    clear_bash_danger_grant_for_tests()


async def test_request_permission_timeout(monkeypatch, tmp_path):
    c = Config(
        llm=LLMConfig(api_key="k", model="m"),
        command_tools=CommandToolsConfig(acl_base_dir=str(tmp_path / "acl")),
    )
    import agent_core.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "get_config", lambda: c, raising=True)
    set_permission_notify_hook(lambda *_: None)
    tool = RequestPermissionTool()
    r = await tool.execute(
        summary="x",
        timeout_seconds=0.2,
        __execution_context__={},
    )
    assert not r.success
    assert r.error == "PERMISSION_TIMEOUT"


async def test_request_permission_clarify(patched_get_config):
    """飞书「精确指令」：clarify_requested 时返回 PERMISSION_CLARIFY。"""
    tool = RequestPermissionTool()
    exec_ctx = {"source": "cli", "user_id": "alice"}
    captured: list[str] = []

    def _notify(pid: str, payload: object) -> None:
        captured.append(pid)

    set_permission_notify_hook(_notify)
    try:

        async def _run():
            return await tool.execute(
                summary="need path",
                kind="file_write",
                timeout_seconds=5.0,
                __execution_context__=exec_ctx,
            )

        task = asyncio.create_task(_run())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if captured:
                break
        assert captured
        pid = captured[0]
        ok = resolve_permission(
            pid,
            PermissionDecision(
                allowed=False,
                clarify_requested=True,
                note="用户要更精确说明",
            ),
        )
        assert ok
        result = await task
        assert not result.success
        assert result.error == "PERMISSION_CLARIFY"
        assert "澄清" in (result.message or "")
        assert result.data.get("user_instruction") == ""
    finally:
        set_permission_notify_hook(None)


async def test_request_permission_ephemeral_skips_acl_file(patched_get_config):
    """persist_acl=False 时不写 writable_roots.json，仅进程内临时前缀。"""
    tmp_path, _c = patched_get_config
    captured: list[str] = []

    def _notify(pid: str, payload: object) -> None:
        captured.append(pid)

    set_permission_notify_hook(_notify)
    tool = RequestPermissionTool()
    exec_ctx = {"source": "cli", "user_id": "alice"}
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    async def _run():
        return await tool.execute(
            summary="write outside",
            kind="file_write",
            details=json.dumps({"path": str(outside / "f.txt")}),
            timeout_seconds=5.0,
            __execution_context__=exec_ctx,
        )

    task = asyncio.create_task(_run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if captured:
            break
    assert captured
    pid = captured[0]
    ok = resolve_permission(
        pid,
        PermissionDecision(
            allowed=True,
            path_prefix=str(outside),
            persist_acl=False,
        ),
    )
    assert ok
    result = await task
    assert result.success
    assert result.data.get("persist_acl") is False
    acl_file = tmp_path / "acl" / "cli" / "alice" / "writable_roots.json"
    assert not acl_file.exists()
    clear_ephemeral_writable_grants_for_tests()
    set_permission_notify_hook(None)


async def test_request_permission_clarify_includes_user_instruction_in_data(
    patched_get_config,
):
    set_permission_notify_hook(lambda *_: None)
    tool = RequestPermissionTool()
    exec_ctx = {"source": "cli", "user_id": "alice"}
    captured: list[str] = []

    def _notify(pid: str, payload: object) -> None:
        captured.append(pid)

    set_permission_notify_hook(_notify)
    try:

        async def _run():
            return await tool.execute(
                summary="x",
                timeout_seconds=5.0,
                __execution_context__=exec_ctx,
            )

        task = asyncio.create_task(_run())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if captured:
                break
        pid = captured[0]
        ok = resolve_permission(
            pid,
            PermissionDecision(
                allowed=False,
                clarify_requested=True,
                user_instruction="仅允许 /tmp/proj",
            ),
        )
        assert ok
        result = await task
        assert result.error == "PERMISSION_CLARIFY"
        assert result.data.get("user_instruction") == "仅允许 /tmp/proj"
        assert "/tmp/proj" in (result.message or "")
    finally:
        set_permission_notify_hook(None)
