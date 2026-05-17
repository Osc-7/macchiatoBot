"""bash_os_user：逻辑用户到 Linux 用户名与 runuser 解析。"""

import grp
import sys
from types import SimpleNamespace

import pytest

from agent_core.bash_os_user import (
    logic_os_user_name,
    memory_owner_key,
    reconcile_admin_linux_users,
    reconcile_admin_sudo_group,
    resolve_admin_system_user,
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


def test_resolve_bash_run_as_user_admin_uses_logic_user() -> None:
    cfg = CommandToolsConfig(
        bash_os_user_enabled=True,
        workspace_isolation_enabled=True,
        workspace_admin_memory_owners=["cli:root"],
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
    assert u == "m_cli_root"


def test_resolve_bash_run_as_user_admin_needs_no_legacy_mapping() -> None:
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
    assert u == "m_cli_root"
    assert reason == "ok"


def test_resolve_bash_run_as_user_profile_admin() -> None:
    cfg = CommandToolsConfig(
        bash_os_user_enabled=True,
        workspace_isolation_enabled=True,
        workspace_admin_memory_owners=[],
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
    assert u == "m_cli_alice"


def test_resolve_admin_system_user() -> None:
    cfg = CommandToolsConfig(
        bash_os_admin_system_users={"cli:root": "mac_admin"},
    )
    assert resolve_admin_system_user(cfg, source="cli", user_id="root") == "mac_admin"
    assert resolve_admin_system_user(cfg, source="cli", user_id="alice") is None


def test_reconcile_admin_sudo_group(monkeypatch) -> None:
    cfg = CommandToolsConfig(
        bash_os_user_enabled=True,
        bash_os_admin_manage_sudo_group=True,
        bash_os_admin_sudo_group="sudo",
        workspace_admin_memory_owners=["cli:root"],
        bash_os_admin_system_users={
            "cli:root": "mac_admin",
            "cli:old": "old_admin",
        },
    )

    called: list[list[str]] = []

    monkeypatch.setattr("agent_core.bash_os_user.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "agent_core.bash_os_user.pwd.getpwnam",
        lambda user: SimpleNamespace(pw_gid=100 if user == "m_cli_root" else 101),
    )
    monkeypatch.setattr(
        "agent_core.bash_os_user.grp.getgrnam",
        lambda group: SimpleNamespace(
            gr_gid=999,
            gr_mem=["mac_admin", "old_admin"],
        ),
    )

    def _run(args, capture_output, text, timeout):
        called.append(args)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("agent_core.bash_os_user.subprocess.run", _run)

    reconcile_admin_sudo_group(cfg)

    assert ["usermod", "-aG", "sudo", "m_cli_root"] in called
    assert ["gpasswd", "-d", "mac_admin", "sudo"] in called
    assert ["gpasswd", "-d", "old_admin", "sudo"] in called


def test_reconcile_admin_linux_users_repairs_home_and_sudoers(tmp_path, monkeypatch) -> None:
    cfg = CommandToolsConfig(
        bash_os_user_enabled=True,
        bash_os_user_home_base_dir=str(tmp_path / "homes"),
        bash_os_admin_manage_sudo_group=False,
        bash_os_admin_manage_sudo_nopasswd=True,
        bash_os_admin_sudoers_dir=str(tmp_path / "sudoers.d"),
        workspace_admin_memory_owners=["cli:root"],
        bash_os_admin_system_users={"cli:root": "mac_admin"},
    )

    home = tmp_path / "homes" / "m_cli_root"
    chowned: list[str] = []
    provisioned: list[tuple[str, str]] = []

    monkeypatch.setattr("agent_core.bash_os_user.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "agent_core.bash_os_user.pwd.getpwnam",
        lambda user: SimpleNamespace(
            pw_gid=1000,
            pw_uid=1000,
            pw_dir=str(home),
        ),
    )
    monkeypatch.setattr(
        "agent_core.bash_os_user.chown_tree_to_user",
        lambda paths, username: chowned.extend(str(p) for p in paths),
    )
    monkeypatch.setattr(
        "agent_core.bash_os_user.provision_system_user",
        lambda username, system, comment, home_dir: provisioned.append(
            (username, str(home_dir))
        ),
    )

    reconcile_admin_linux_users(cfg)

    assert ("m_cli_root", str(home)) in provisioned
    assert str(home) in chowned
    sudoers = tmp_path / "sudoers.d" / "macchiato-m_cli_root"
    assert sudoers.read_text(encoding="utf-8") == "m_cli_root ALL=(ALL) NOPASSWD:ALL\n"
    assert oct(sudoers.stat().st_mode & 0o777) == "0o440"


def test_reconcile_admin_linux_users_removes_stale_sudoers(tmp_path, monkeypatch) -> None:
    cfg = CommandToolsConfig(
        bash_os_user_enabled=True,
        bash_os_admin_manage_sudo_group=False,
        bash_os_admin_manage_sudo_nopasswd=True,
        bash_os_admin_sudoers_dir=str(tmp_path / "sudoers.d"),
        workspace_admin_memory_owners=[],
        bash_os_admin_system_users={"cli:root": "mac_admin"},
    )
    sudoers = tmp_path / "sudoers.d" / "macchiato-mac_admin"
    sudoers.parent.mkdir(parents=True, exist_ok=True)
    sudoers.write_text("stale\n", encoding="utf-8")

    monkeypatch.setattr("agent_core.bash_os_user.os.geteuid", lambda: 0)

    reconcile_admin_linux_users(cfg)

    assert not sudoers.exists()
