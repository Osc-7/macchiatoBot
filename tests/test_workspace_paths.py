"""workspace_paths: bash 工作区目录布局。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.agent.workspace_paths import (
    build_bash_workspace_guard_init,
    ensure_workspace_owner_layout,
    is_bash_workspace_admin,
    list_user_ids_under_workspace,
    resolve_bash_working_dir,
    resolve_workspace_owner_dir,
    resolve_workspace_tmp_dir,
)
from agent_core.config import CommandToolsConfig
from agent_core.kernel_interface.profile import CoreProfile


def test_ensure_workspace_owner_layout_creates_and_idempotent(tmp_path) -> None:
    cfg = CommandToolsConfig(workspace_base_dir=str(tmp_path / "w"))
    r1 = ensure_workspace_owner_layout(cfg, "alice", source="cli")
    r2 = ensure_workspace_owner_layout(cfg, "alice", source="cli")
    assert r1["workspace_owner"] == "cli:alice"
    assert (tmp_path / "w" / "cli" / "alice").is_dir()
    assert Path(r1["tmp_dir"]).is_dir()
    assert len(r1["created_paths"]) >= 1
    assert r2["created_paths"] == []


def test_list_user_ids_under_workspace(tmp_path) -> None:
    cfg = CommandToolsConfig(workspace_base_dir=str(tmp_path / "w"))
    ensure_workspace_owner_layout(cfg, "u1", source="cli")
    ensure_workspace_owner_layout(cfg, "u2", source="cli")
    ids = list_user_ids_under_workspace(cfg, frontend="cli")
    assert set(ids) == {"u1", "u2"}


def test_resolve_workspace_owner_dir(tmp_path) -> None:
    cfg = CommandToolsConfig(workspace_base_dir=str(tmp_path / "w"))
    p = resolve_workspace_owner_dir(cfg, "bob", source="feishu")
    assert p == str(tmp_path / "w" / "feishu" / "bob")


def test_resolve_workspace_tmp_dir() -> None:
    cfg = CommandToolsConfig()
    p = resolve_workspace_tmp_dir(cfg, "bob", source="feishu")
    assert p == "/tmp/macchiato/feishu/bob"


def test_resolve_bash_working_dir_isolated(tmp_path) -> None:
    cfg = CommandToolsConfig(
        workspace_base_dir=str(tmp_path / "w"),
        workspace_isolation_enabled=True,
        workspace_admin_memory_owners=[],
        base_dir=".",
    )
    d = resolve_bash_working_dir(cfg, "root", source="cli", profile=None)
    assert d == str(tmp_path / "w" / "cli" / "root")


def test_resolve_bash_working_dir_config_admin_list(tmp_path) -> None:
    cfg = CommandToolsConfig(
        workspace_base_dir=str(tmp_path / "w"),
        workspace_isolation_enabled=True,
        workspace_admin_memory_owners=["cli:root"],
        base_dir="/tmp/project",
    )
    d = resolve_bash_working_dir(cfg, "root", source="cli", profile=None)
    assert d == "/tmp/project"


def test_resolve_bash_working_dir_profile_admin(tmp_path) -> None:
    cfg = CommandToolsConfig(
        workspace_base_dir=str(tmp_path / "w"),
        workspace_isolation_enabled=True,
        workspace_admin_memory_owners=[],
        base_dir="/srv/app",
    )
    prof = CoreProfile(bash_workspace_admin=True)
    d = resolve_bash_working_dir(cfg, "root", source="cli", profile=prof)
    assert d == "/srv/app"


def test_resolve_bash_working_dir_isolation_off(tmp_path) -> None:
    cfg = CommandToolsConfig(
        workspace_base_dir=str(tmp_path / "w"),
        workspace_isolation_enabled=False,
        workspace_admin_memory_owners=[],
        base_dir=".",
    )
    d = resolve_bash_working_dir(cfg, "root", source="cli")
    assert d == "."


def test_is_bash_workspace_admin_profile_overrides_empty_list(tmp_path) -> None:
    cfg = CommandToolsConfig(
        workspace_base_dir=str(tmp_path / "w"),
        workspace_admin_memory_owners=[],
    )
    assert is_bash_workspace_admin(
        cfg, "cli", "root", CoreProfile(bash_workspace_admin=True)
    )


def test_build_bash_workspace_guard_init_contains_root(tmp_path) -> None:
    root = str(tmp_path / "ws" / "cli" / "u1")
    lines = build_bash_workspace_guard_init(root)
    assert len(lines) == 1
    assert "MACCHIATO_WORKSPACE_ROOT=" in lines[0]
    assert "cd()" in lines[0]


def test_validate_rejects_bad_user_for_workspace(tmp_path) -> None:
    cfg = CommandToolsConfig(workspace_base_dir=str(tmp_path / "w"))
    with pytest.raises(ValueError):
        ensure_workspace_owner_layout(cfg, "../x", source="cli")
