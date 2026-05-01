"""
Bash 会话以 Linux 系统用户（runuser）运行：租户 / 管理员与 config 对齐。

- 租户：稳定可读 POSIX 名（过长时用短 hash 保证唯一与长度上限）。
- 管理员：由 ``bash_os_admin_system_users`` 将 memory_owner（如 cli:root）映射到已有系统账号。
"""

from __future__ import annotations

import hashlib
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
    """
    租户是否以 Linux home 作为 canonical workspace/memory 根。

    管理员 Core 仍保留项目根 / 显式映射用户的行为，不迁入专属 home。
    """
    if not getattr(cmd_cfg, "bash_os_user_enabled", False):
        return False
    from agent_core.agent.workspace_paths import is_bash_workspace_admin

    return not is_bash_workspace_admin(cmd_cfg, source, user_id, profile)


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
    key = memory_owner_key(source, user_id)
    if admin:
        mapping: Dict[str, str] = getattr(cmd_cfg, "bash_os_admin_system_users", {}) or {}
        name = (mapping.get(key) or "").strip()
        if not name:
            logger.warning(
                "bash_os_user_enabled: admin Core %s has no bash_os_admin_system_users entry; "
                "running bash as service UID",
                key,
            )
            return None, "admin_not_mapped"
        return name, "ok"

    if not ws_restricted:
        return None, "not_isolated"

    prefix = getattr(cmd_cfg, "bash_os_tenant_user_prefix", "m_")
    return logic_os_user_name(source, user_id, prefix=str(prefix)), "ok"
