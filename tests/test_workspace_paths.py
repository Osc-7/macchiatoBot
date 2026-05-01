"""workspace_paths: bash 工作区目录布局。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.agent.workspace_paths import (
    build_bash_workspace_guard_init,
    ensure_workspace_data_memory_symlink,
    ensure_workspace_owner_layout,
    is_bash_workspace_admin,
    list_user_ids_under_workspace,
    migrate_legacy_workspace_and_memory_to_home,
    resolve_bash_working_dir,
    resolve_workspace_owner_dir,
    resolve_workspace_tmp_dir,
)
from agent_core.config import CommandToolsConfig, Config, LLMConfig, MemoryConfig
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


def test_resolve_workspace_owner_dir_uses_linux_home_for_tenant(tmp_path) -> None:
    cfg = CommandToolsConfig(
        workspace_base_dir=str(tmp_path / "w"),
        bash_os_user_enabled=True,
        bash_os_user_home_base_dir=str(tmp_path / "homes"),
    )
    p = resolve_workspace_owner_dir(cfg, "bob", source="feishu")
    assert p == str(tmp_path / "homes" / "m_feishu_bob")


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
    lines = build_bash_workspace_guard_init(
        root,
        project_root="/proj",
        memory_long_term_dir="/proj/data/memory/cli/u1/long_term",
        memory_owner_dir="/proj/data/memory/cli/u1",
    )
    assert len(lines) == 1
    script = lines[0]
    assert "MACCHIATO_REAL_HOME=" in script
    assert "MACCHIATO_WORKSPACE_ROOT=" in script
    assert "MACCHIATO_USER_ROOT=" in script
    assert "MACCHIATO_PROJECT_ROOT=" in script
    assert ".sandbox_home" not in script
    assert 'export HOME="$MACCHIATO_WORKSPACE_ROOT"' in script
    assert "MACCHIATO_MEMORY_LONG_TERM=" in script
    assert "MACCHIATO_MEMORY_OWNER_DIR=" in script
    assert "_macchiato_path_if_dir" in script
    assert "node_modules/.bin" in script
    assert ".nvm/versions/node" in script
    assert "XDG_CONFIG_HOME" in script
    assert '$HOME/.local/bin' in script
    assert "MACCHIATO_PROJECT_ROOT" in script
    ws_bin = script.find("${MACCHIATO_WORKSPACE_ROOT:-}/node_modules/.bin")
    pr_bin = script.find("${MACCHIATO_PROJECT_ROOT:-}/node_modules/.bin")
    assert ws_bin != -1 and pr_bin != -1 and ws_bin < pr_bin
    assert "cd()" in script


def test_build_bash_workspace_guard_init_no_jail_cd(tmp_path) -> None:
    root = str(tmp_path / "ws" / "cli" / "u1")
    lines = build_bash_workspace_guard_init(
        root,
        project_root="/proj",
        jail_cd=False,
    )
    script = lines[0]
    assert "MACCHIATO_WORKSPACE_ROOT=" in script
    assert "cd()" not in script


def test_build_bash_admin_bootstrap_init_no_cd_override(tmp_path) -> None:
    from agent_core.agent.workspace_paths import build_bash_admin_bootstrap_init

    base = str(tmp_path / "repo")
    Path(base).mkdir(parents=True)
    lines = build_bash_admin_bootstrap_init(base, project_root="/proj")
    script = lines[0]
    assert "MACCHIATO_PROJECT_ROOT=" in script
    assert "cd()" not in script


def test_ensure_workspace_data_memory_symlink_grafts(tmp_path) -> None:
    pr = tmp_path / "repo"
    (pr / "data" / "memory" / "cli" / "u1").mkdir(parents=True)
    owner = pr / "data" / "workspace" / "cli" / "u1"
    owner.mkdir(parents=True)
    ensure_workspace_data_memory_symlink(owner, project_root=pr, source="cli", user_id="u1")
    link = owner / "data" / "memory"
    assert link.is_symlink()
    assert link.resolve() == (pr / "data" / "memory" / "cli" / "u1").resolve()


def test_ensure_workspace_owner_layout_uses_home_memory_layout(tmp_path) -> None:
    cfg = Config(
        llm=LLMConfig(api_key="t", model="t"),
        memory=MemoryConfig(memory_base_dir=str(tmp_path / "legacy-memory")),
        command_tools=CommandToolsConfig(
            workspace_base_dir=str(tmp_path / "legacy-ws"),
            workspace_isolation_enabled=True,
            bash_os_user_enabled=True,
            bash_os_user_home_base_dir=str(tmp_path / "homes"),
        ),
    )
    layout = ensure_workspace_owner_layout(
        cfg.command_tools,
        "u1",
        source="feishu",
        app_config=cfg,
    )
    owner = tmp_path / "homes" / "m_feishu_u1"
    assert layout["owner_dir"] == str(owner)
    assert owner.is_dir()
    assert (owner / "data" / "memory" / "long_term").is_dir()
    assert not (owner / "data" / "memory").is_symlink()


def test_migrate_legacy_workspace_and_memory_to_home(tmp_path) -> None:
    legacy_ws = tmp_path / "legacy-ws" / "feishu" / "u1"
    legacy_mem = tmp_path / "legacy-mem" / "feishu" / "u1"
    (legacy_ws / "notes").mkdir(parents=True)
    (legacy_ws / "notes" / "todo.txt").write_text("workspace", encoding="utf-8")
    (legacy_mem / "long_term").mkdir(parents=True)
    (legacy_mem / "long_term" / "MEMORY.md").write_text("memory", encoding="utf-8")
    cmd = CommandToolsConfig(
        workspace_base_dir=str(tmp_path / "legacy-ws"),
        bash_os_user_enabled=True,
        bash_os_user_home_base_dir=str(tmp_path / "homes"),
    )
    mem = MemoryConfig(memory_base_dir=str(tmp_path / "legacy-mem"))
    result = migrate_legacy_workspace_and_memory_to_home(
        cmd,
        mem,
        source="feishu",
        user_id="u1",
    )
    home = tmp_path / "homes" / "m_feishu_u1"
    assert result["migrated"]
    assert (home / "notes" / "todo.txt").read_text(encoding="utf-8") == "workspace"
    assert (home / "data" / "memory" / "long_term" / "MEMORY.md").read_text(
        encoding="utf-8"
    ) == "memory"


def test_validate_rejects_bad_user_for_workspace(tmp_path) -> None:
    cfg = CommandToolsConfig(workspace_base_dir=str(tmp_path / "w"))
    with pytest.raises(ValueError):
        ensure_workspace_owner_layout(cfg, "../x", source="cli")
