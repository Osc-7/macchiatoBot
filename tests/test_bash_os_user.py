"""bash_os_user：逻辑用户到 Linux 用户名与 runuser 解析。"""

import sys

import pytest

from agent_core.bash_os_user import (
    logic_os_user_name,
    memory_owner_key,
    resolve_os_user_home,
    resolve_bash_run_as_user,
)
from agent_core.config import CommandToolsConfig
from agent_core.kernel_interface.profile import CoreProfile


def test_memory_owner_key() -> None:
    assert memory_owner_key("cli", "root") == "cli:root"
    assert memory_owner_key("feishu", "ou_abc") == "feishu:ou_abc"


def test_logic_os_user_name_short() -> None:
    n = logic_os_user_name("cli", "alice", prefix="m_")
    assert n.startswith("m_")
    assert "cli" in n or "alice" in n
    assert len(n) <= 31


def test_logic_os_user_name_long_user_id_stable() -> None:
    long_uid = "u" * 100
    a = logic_os_user_name("feishu", long_uid, prefix="m_")
    b = logic_os_user_name("feishu", long_uid, prefix="m_")
    assert a == b
    assert len(a) <= 31


def test_resolve_os_user_home_uses_configured_base(tmp_path) -> None:
    cfg = CommandToolsConfig(
        bash_os_user_enabled=True,
        bash_os_user_home_base_dir=str(tmp_path / "homes"),
    )
    home = resolve_os_user_home(cfg, "m_cli_alice")
    assert home == (tmp_path / "homes" / "m_cli_alice").resolve()


def test_resolve_bash_run_as_user_disabled() -> None:
    cfg = CommandToolsConfig(bash_os_user_enabled=False)
    u, reason = resolve_bash_run_as_user(
        cfg, source="cli", user_id="x", ws_restricted=True, profile=None
    )
    assert u is None
    assert reason == "os_user_disabled"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="runuser path Linux-only")
def test_resolve_bash_run_as_user_tenant_when_enabled() -> None:
    cfg = CommandToolsConfig(
        bash_os_user_enabled=True,
        workspace_isolation_enabled=True,
        workspace_admin_memory_owners=[],
    )
    u, reason = resolve_bash_run_as_user(
        cfg, source="cli", user_id="bob", ws_restricted=True, profile=None
    )
    if reason == "runuser_missing":
        pytest.skip("no /sbin/runuser in environment")
    assert reason == "ok"
    assert u is not None
    assert u.startswith("m_")


def test_resolve_bash_run_as_user_admin_mapping() -> None:
    cfg = CommandToolsConfig(
        bash_os_user_enabled=True,
        workspace_isolation_enabled=True,
        workspace_admin_memory_owners=["cli:root"],
        bash_os_admin_system_users={"cli:root": "macchiato_admin"},
    )
    u, reason = resolve_bash_run_as_user(
        cfg,
        source="cli",
        user_id="root",
        ws_restricted=False,
        profile=None,
    )
    if not sys.platform.startswith("linux"):
        assert u is None
        return
    if reason == "runuser_missing":
        pytest.skip("no /sbin/runuser in environment")
    assert reason == "ok"
    assert u == "macchiato_admin"


def test_resolve_bash_run_as_user_admin_unmapped() -> None:
    from agent_core.bash_os_user import runuser_available

    if not sys.platform.startswith("linux") or not runuser_available("/sbin/runuser"):
        pytest.skip("need Linux with runuser")
    cfg = CommandToolsConfig(
        bash_os_user_enabled=True,
        workspace_isolation_enabled=True,
        workspace_admin_memory_owners=["cli:root"],
        bash_os_admin_system_users={},
    )
    u, reason = resolve_bash_run_as_user(
        cfg,
        source="cli",
        user_id="root",
        ws_restricted=False,
        profile=None,
    )
    assert u is None
    assert reason == "admin_not_mapped"


def test_resolve_bash_run_as_user_profile_admin() -> None:
    cfg = CommandToolsConfig(
        bash_os_user_enabled=True,
        workspace_isolation_enabled=True,
        workspace_admin_memory_owners=[],
        bash_os_admin_system_users={"cli:alice": "mac_admin"},
    )
    u, reason = resolve_bash_run_as_user(
        cfg,
        source="cli",
        user_id="alice",
        ws_restricted=True,
        profile=CoreProfile(bash_workspace_admin=True),
    )
    if not sys.platform.startswith("linux"):
        assert u is None
        return
    if reason == "runuser_missing":
        pytest.skip("no /sbin/runuser in environment")
    assert reason == "ok"
    assert u == "mac_admin"
