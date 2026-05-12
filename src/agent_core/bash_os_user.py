"""
Bash 会话以 Linux 系统用户（runuser）运行：每个逻辑用户对应一个稳定 Linux 用户。

管理员能力是该逻辑 Linux 用户上的权限（sudoers / sudo group），而不是切换到另一个
共享管理员账号；这样同一个 Core 的 HOME、技能目录、文件工具与 bash 用户保持一致。
"""

from __future__ import annotations

import hashlib
import grp
import logging
import os
import pwd
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from agent_core.agent.memory_paths import validate_logic_namespace_segment

if TYPE_CHECKING:
    from agent_core.config import CommandToolsConfig

logger = logging.getLogger(__name__)

# Linux 用户名常用上限（含 systemd 等），保守取 31
POSIX_USER_NAME_MAX = 31

_SAFE_POSIX_BODY = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _sanitize_segment(s: str, *, max_len: int) -> str:
    t = (s or "").strip().lower()
    t = re.sub(r"[^a-z0-9_-]+", "_", t)
    t = re.sub(r"_+", "_", t).strip("_")
    if not t:
        t = "u"
    if not t[0].isalpha() and t[0] != "_":
        t = "u" + t
    if len(t) > max_len:
        t = t[:max_len].rstrip("_") or "u"
    return t


def logic_os_user_name(
    source: str,
    user_id: str,
    *,
    prefix: str = "m_",
    max_len: int = POSIX_USER_NAME_MAX,
) -> str:
    """
    将 (source, user_id) 映射为可审计的 POSIX 用户名，稳定且幂等。

    优先 ``{prefix}{frontend}_{user}``；超长则用 ``{prefix}{frontend}_{digest8}`` 等缩短形式。
    """
    fe = validate_logic_namespace_segment(
        (source or "").strip() or "cli", what="frontend"
    )
    uid = validate_logic_namespace_segment(
        (user_id or "").strip() or "root", what="user_id"
    )
    p = (prefix or "m_").strip() or "m_"
    if not p.endswith("_") and not p[-1].isalnum():
        p = p + "_"
    fe_s = _sanitize_segment(fe, max_len=16)
    uid_s = _sanitize_segment(uid, max_len=max_len)  # trimmed in candidate check

    digest8 = hashlib.sha256(f"{fe}\0{uid}".encode()).hexdigest()[:8]
    budget = max_len - len(p)
    if budget < 6:
        return (p + digest8)[:max_len]

    candidate = f"{p}{fe_s}_{uid_s}"
    if len(candidate) <= max_len and _SAFE_POSIX_BODY.match(candidate[len(p) :]):
        return candidate

    short = f"{p}{fe_s}_{digest8}"
    if len(short) <= max_len:
        return short
    return (p + digest8)[:max_len]


def memory_owner_key(source: str, user_id: str) -> str:
    fe = validate_logic_namespace_segment(
        (source or "").strip() or "cli", what="frontend"
    )
    uid = validate_logic_namespace_segment(
        (user_id or "").strip() or "root", what="user_id"
    )
    return f"{fe}:{uid}"


def resolve_admin_system_user(
    cmd_cfg: "CommandToolsConfig",
    *,
    source: str,
    user_id: str,
) -> Optional[str]:
    key = memory_owner_key(source, user_id)
    mapping: Dict[str, str] = getattr(cmd_cfg, "bash_os_admin_system_users", {}) or {}
    name = (mapping.get(key) or "").strip()
    return name or None


def runuser_available(runuser_path: str) -> bool:
    p = shutil.which(runuser_path) if not os.path.isabs(runuser_path) else runuser_path
    if not p:
        return False
    return os.path.isfile(p) and os.access(p, os.X_OK)


def platform_supports_runuser() -> bool:
    return sys.platform.startswith("linux")


def provision_system_user(
    posix_name: str,
    *,
    system: bool = True,
    comment: str = "macchiato bash",
    home_dir: Optional[Path] = None,
) -> None:
    """若系统用户不存在则 useradd（幂等）。需 root。"""
    check = subprocess.run(
        ["id", "-u", posix_name],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if check.returncode == 0:
        return
    args: List[str] = ["useradd", "-m", "-c", comment]
    if home_dir is not None:
        args.extend(["-d", str(home_dir)])
    if system:
        args.insert(1, "-r")
    args.append(posix_name)
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"useradd {posix_name} failed: {err}")


def chown_tree_to_user(paths: List[Path], posix_name: str) -> None:
    """将路径及其内容递归 chown 为 posix 用户的主 UID/GID。"""
    try:
        pw = pwd.getpwnam(posix_name)
    except KeyError as exc:
        raise RuntimeError(f"unknown system user: {posix_name}") from exc
    uid, gid = pw.pw_uid, pw.pw_gid

    for base in paths:
        if not base.exists():
            continue
        base_r = base.resolve()
        if not base_r.exists():
            continue
        for root, dirs, files in os.walk(base_r, topdown=False, followlinks=False):
            for name in files:
                p = Path(root) / name
                try:
                    os.chown(p, uid, gid, follow_symlinks=False)
                except OSError:
                    pass
            for name in dirs:
                p = Path(root) / name
                try:
                    os.chown(p, uid, gid, follow_symlinks=False)
                except OSError:
                    pass
        try:
            os.chown(base_r, uid, gid, follow_symlinks=False)
        except OSError:
            pass


def minimal_subprocess_env_for_runuser(
    *,
    cwd: Path,
    project_root: Path,
    macchiato_real_home: Path,
    home_for_session: Optional[Path] = None,
) -> Dict[str, str]:
    """
    传给 runuser 子进程的环境（不全量继承服务进程，避免泄露机密 env）。

    子 shell 内仍由 workspace / admin 启动脚本导出 MACCHIATO_* 与 PATH 片段。
    ``home_for_session``：会话 ``HOME``/``PWD`` 初始值；默认与 ``cwd`` 相同（租户工作区）。
    """
    home = home_for_session if home_for_session is not None else cwd
    base = {k: v for k, v in os.environ.items() if k in ("LANG", "LC_ALL", "LC_CTYPE")}
    base.setdefault("LANG", "C.UTF-8")
    base.update(
        {
            "MACCHIATO_BASH": "1",
            "HOME": str(home),
            "PWD": str(cwd),
            "USER": os.environ.get("USER", ""),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "TERM": os.environ.get("TERM", "dumb"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "MACCHIATO_PROJECT_ROOT": str(project_root.resolve()),
            "MACCHIATO_REAL_HOME": str(macchiato_real_home.resolve()),
        }
    )
    return base


def resolve_os_user_home(
    cmd_cfg: "CommandToolsConfig",
    posix_name: str,
) -> Path:
    """解析 Linux 用户 home；若系统中尚不存在则回退到配置的 home base。"""
    try:
        return Path(pwd.getpwnam(posix_name).pw_dir).resolve()
    except KeyError:
        pass
    home_base = Path(
        (getattr(cmd_cfg, "bash_os_user_home_base_dir", "/home") or "/home").strip()
    ).expanduser()
    return (home_base / posix_name).resolve()


def should_use_os_home_for_logic_user(
    cmd_cfg: "CommandToolsConfig",
    *,
    source: str,
    user_id: str,
    profile: Optional[Any] = None,
) -> bool:
    """启用 Linux 用户隔离时，逻辑用户的 Linux home 即 canonical workspace/memory 根。"""
    return bool(getattr(cmd_cfg, "bash_os_user_enabled", False))


def _user_in_group(username: str, group_name: str) -> bool:
    try:
        pw = pwd.getpwnam(username)
        gr = grp.getgrnam(group_name)
    except KeyError:
        return False
    if pw.pw_gid == gr.gr_gid:
        return True
    return username in set(gr.gr_mem or [])


def _sudoers_dropin_path(cmd_cfg: "CommandToolsConfig", username: str) -> Path:
    base = Path(
        (getattr(cmd_cfg, "bash_os_admin_sudoers_dir", "/etc/sudoers.d") or "/etc/sudoers.d").strip()
    ).expanduser()
    safe_user = re.sub(r"[^a-zA-Z0-9_.-]+", "_", username).strip("._") or "user"
    return base / f"macchiato-{safe_user}"


def _ensure_admin_home_owned(cmd_cfg: "CommandToolsConfig", username: str) -> None:
    home = resolve_os_user_home(cmd_cfg, username)
    home.mkdir(parents=True, exist_ok=True)
    chown_tree_to_user([home], username)


def _write_admin_sudoers_dropin(cmd_cfg: "CommandToolsConfig", username: str) -> None:
    path = _sudoers_dropin_path(cmd_cfg, username)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f"{username} ALL=(ALL) NOPASSWD:ALL\n"
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o440)


def _remove_admin_sudoers_dropin(cmd_cfg: "CommandToolsConfig", username: str) -> None:
    path = _sudoers_dropin_path(cmd_cfg, username)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning(
            "reconcile_admin_linux_users: failed to remove sudoers drop-in %s: %s",
            path,
            exc,
        )


def _admin_logic_usernames(cmd_cfg: "CommandToolsConfig") -> Dict[str, str]:
    """Return workspace-admin memory owners mapped to their own logic Linux usernames."""
    out: Dict[str, str] = {}
    prefix = str(getattr(cmd_cfg, "bash_os_tenant_user_prefix", "m_"))
    for owner in getattr(cmd_cfg, "workspace_admin_memory_owners", []) or []:
        key = (owner or "").strip()
        if not key or ":" not in key:
            continue
        source, user_id = key.split(":", 1)
        try:
            out[key] = logic_os_user_name(source, user_id, prefix=prefix)
        except Exception as exc:
            logger.warning("invalid workspace admin owner %s: %s", key, exc)
    return out


def reconcile_admin_sudo_group(cmd_cfg: "CommandToolsConfig") -> None:
    """root daemon 启动时按管理员逻辑用户对账 sudo group 成员。"""
    if not getattr(cmd_cfg, "bash_os_user_enabled", False):
        return
    if not getattr(cmd_cfg, "bash_os_admin_manage_sudo_group", True):
        return
    if os.geteuid() != 0:
        logger.debug("skip reconcile_admin_sudo_group: not running as root")
        return

    group_name = (
        getattr(cmd_cfg, "bash_os_admin_sudo_group", "sudo") or "sudo"
    ).strip()
    if not group_name:
        return

    desired_usernames = set(_admin_logic_usernames(cmd_cfg).values())
    legacy_usernames = {
        user.strip()
        for user in (getattr(cmd_cfg, "bash_os_admin_system_users", {}) or {}).values()
        if user and user.strip()
    }

    for username in sorted(desired_usernames):
        in_group = _user_in_group(username, group_name)
        if not in_group:
            proc = subprocess.run(
                ["usermod", "-aG", group_name, username],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                logger.warning(
                    "reconcile_admin_sudo_group: failed to add %s to %s: %s",
                    username,
                    group_name,
                    err,
                )

    for username in sorted(legacy_usernames - desired_usernames):
        if _user_in_group(username, group_name):
            proc = subprocess.run(
                ["gpasswd", "-d", username, group_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                logger.warning(
                    "reconcile_admin_sudo_group: failed to remove %s from %s: %s",
                    username,
                    group_name,
                    err,
                )


def reconcile_admin_linux_users(cmd_cfg: "CommandToolsConfig") -> None:
    """root daemon 启动时统一对账管理员逻辑用户的 home / sudo group / sudoers drop-in。"""
    if not getattr(cmd_cfg, "bash_os_user_enabled", False):
        return
    if os.geteuid() != 0:
        logger.debug("skip reconcile_admin_linux_users: not running as root")
        return

    desired_usernames = set(_admin_logic_usernames(cmd_cfg).values())

    for username in sorted(desired_usernames):
        try:
            home = resolve_os_user_home(cmd_cfg, username)
            if getattr(cmd_cfg, "bash_os_auto_provision_users", True):
                provision_system_user(
                    username,
                    system=True,
                    comment="macchiato bash admin",
                    home_dir=home,
                )
            _ensure_admin_home_owned(cmd_cfg, username)
        except Exception as exc:
            logger.warning(
                "reconcile_admin_linux_users: failed to ensure admin logic user %s: %s",
                username,
                exc,
            )

        if getattr(cmd_cfg, "bash_os_admin_manage_sudo_nopasswd", True):
            try:
                _write_admin_sudoers_dropin(cmd_cfg, username)
            except Exception as exc:
                logger.warning(
                    "reconcile_admin_linux_users: failed to write sudoers drop-in for %s: %s",
                    username,
                    exc,
                )

    if getattr(cmd_cfg, "bash_os_admin_manage_sudo_nopasswd", True):
        legacy_usernames = {
            user.strip()
            for user in (getattr(cmd_cfg, "bash_os_admin_system_users", {}) or {}).values()
            if user and user.strip()
        }
        for username in sorted(legacy_usernames - desired_usernames):
            _remove_admin_sudoers_dropin(cmd_cfg, username)

    reconcile_admin_sudo_group(cmd_cfg)


def resolve_bash_run_as_user(
    cmd_cfg: "CommandToolsConfig",
    *,
    source: str,
    user_id: str,
    ws_restricted: bool,
    profile: Optional[Any] = None,
) -> Tuple[Optional[str], str]:
    """
    返回 (posix_name, skip_reason)。

    skip_reason 为 ``ok`` 表示应使用 runuser；否则为简短机器可读原因（未启用、非 Linux、无 runuser 等）。
    """
    from agent_core.agent.workspace_paths import is_bash_workspace_admin

    if not getattr(cmd_cfg, "bash_os_user_enabled", False):
        return None, "os_user_disabled"
    if not platform_supports_runuser():
        return None, "not_linux"
    ru = getattr(cmd_cfg, "bash_runuser_path", "/sbin/runuser")
    if not runuser_available(ru):
        logger.warning("bash_os_user_enabled but runuser not executable: %s", ru)
        return None, "runuser_missing"

    admin = is_bash_workspace_admin(cmd_cfg, source, user_id, profile)
    if not ws_restricted and not admin:
        return None, "not_isolated"

    prefix = getattr(cmd_cfg, "bash_os_tenant_user_prefix", "m_")
    return logic_os_user_name(source, user_id, prefix=str(prefix)), "ok"
