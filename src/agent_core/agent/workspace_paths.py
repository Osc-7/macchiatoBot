"""Bash 工作区路径：按 frontend / user 划分，与 data/memory 布局一致。"""

from __future__ import annotations

import logging
import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent_core.config import CommandToolsConfig

from .memory_paths import resolve_memory_owner_paths, validate_logic_namespace_segment
from .writable_roots_store import load_user_writable_prefixes

if TYPE_CHECKING:
    from agent_core.config import Config

logger = logging.getLogger(__name__)

_TMP_BASE_DIR = Path("/tmp/macchiato")

# workspace_paths.py -> agent/ -> agent_core/ -> src/ -> 仓库根
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def resolve_project_root() -> Path:
    """仓库根目录（含 config.yaml、src/ 的目录）。"""
    return _PROJECT_ROOT


def resolve_configured_write_roots(entries: List[str], *, project_root: Path) -> List[Path]:
    """将配置中的可写根（~、相对仓库根）解析为绝对 Path。"""
    out: List[Path] = []
    for e in entries or []:
        s = (e or "").strip()
        if not s:
            continue
        p = Path(s).expanduser()
        if not p.is_absolute():
            p = (project_root / p).resolve()
        else:
            p = p.resolve()
        out.append(p)
    return out


def merged_bash_write_root_paths(
    cmd_cfg: CommandToolsConfig,
    source: str,
    user_id: str,
    *,
    app_config: Optional["Config"] = None,
    include_canonical_memory_owner: bool = True,
) -> List[Path]:
    """全局 bash_extra_write_roots + 每用户 ACL + 可选真实 data/memory/{fe}/{uid}，去重后供 BashSecurity/file_tools。"""
    pr = resolve_project_root().resolve()
    from_cfg = resolve_configured_write_roots(
        list(cmd_cfg.bash_extra_write_roots or []),
        project_root=pr,
    )
    user_strs = load_user_writable_prefixes(
        cmd_cfg.acl_base_dir, source, user_id, config=app_config
    )
    user_paths = [Path(p).resolve() for p in user_strs]
    mem_paths: List[Path] = []
    if include_canonical_memory_owner and app_config is not None:
        mem = getattr(app_config, "memory", None)
        if mem is not None and getattr(mem, "enabled", True):
            try:
                mp = resolve_memory_owner_paths(mem, user_id, config=app_config, source=source)
                mem_paths.append(Path(mp["chat_history_db_path"]).parent.resolve())
            except Exception:
                pass
    seen: set[str] = set()
    merged: List[Path] = []
    for p in from_cfg + user_paths + mem_paths:
        key = str(p)
        if key not in seen:
            seen.add(key)
            merged.append(p)
    return merged


def ensure_workspace_data_memory_symlink(
    owner_dir: Path,
    *,
    project_root: Optional[Path] = None,
    source: str = "cli",
    user_id: str = "root",
) -> None:
    """
    在工作区下创建 ``data/memory`` -> **仅当前用户**的 ``data/memory/{frontend}/{user_id}/``。

    历史上曾指向整棵 ``data/memory``，导致在用户目录里能看到所有前端子目录，像「整仓数据搬进工作区」；
    现改为只嫁接本会话 owner 目录，与 ``MACCHIATO_MEMORY_*`` 语义一致。

    在用户根下相对路径请用 ``data/memory/long_term``、``content`` 等（不要再叠一层 ``data/memory/feishu/...``）。
    """
    import os

    pr = (project_root or resolve_project_root()).resolve()
    fe = validate_logic_namespace_segment(_ns_segment(source, "cli"), what="frontend")
    uid = validate_logic_namespace_segment(_ns_segment(user_id, "root"), what="user_id")
    target = (pr / "data" / "memory" / fe / uid).resolve()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("ensure_workspace_data_memory_symlink: cannot mkdir target %s: %s", target, exc)
        return

    data_sub = owner_dir / "data"
    try:
        data_sub.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("ensure_workspace_data_memory_symlink: cannot mkdir %s: %s", data_sub, exc)
        return

    link = data_sub / "memory"
    if link.is_symlink():
        try:
            if link.resolve() == target:
                return
        except OSError:
            pass
        try:
            link.unlink()
        except OSError as exc:
            logger.warning("ensure_workspace_data_memory_symlink: cannot replace symlink %s: %s", link, exc)
            return
    elif link.exists():
        logger.warning(
            "ensure_workspace_data_memory_symlink: %s exists and is not a symlink; skip graft",
            link,
        )
        return

    try:
        rel = os.path.relpath(str(target), str(data_sub))
        os.symlink(rel, link, target_is_directory=True)
        logger.info("grafted workspace data/memory symlink %s -> %s", link, rel)
    except OSError as exc:
        logger.warning("ensure_workspace_data_memory_symlink: symlink failed %s: %s", link, exc)


def _ns_segment(value: str, default: str) -> str:
    return (value or "").strip() or default


def resolve_workspace_owner_dir(
    cmd_cfg: CommandToolsConfig,
    user_id: str,
    *,
    source: str = "cli",
) -> str:
    """返回 {workspace_base_dir}/{frontend}/{user_id}/ 目录路径字符串。"""
    fe = validate_logic_namespace_segment(_ns_segment(source, "cli"), what="frontend")
    uid = validate_logic_namespace_segment(_ns_segment(user_id, "root"), what="user_id")
    base = Path((cmd_cfg.workspace_base_dir or "./data/workspace").strip())
    return str(base / fe / uid)


def resolve_workspace_tmp_dir(
    cmd_cfg: CommandToolsConfig,
    user_id: str,
    *,
    source: str = "cli",
) -> str:
    """返回 /tmp/macchiato/{frontend}/{user_id}/ 目录路径字符串。"""
    fe = validate_logic_namespace_segment(_ns_segment(source, "cli"), what="frontend")
    uid = validate_logic_namespace_segment(_ns_segment(user_id, "root"), what="user_id")
    return str(_TMP_BASE_DIR / fe / uid)


def ensure_workspace_owner_layout(
    cmd_cfg: CommandToolsConfig,
    user_id: str,
    *,
    source: str = "cli",
) -> Dict[str, Any]:
    """
    在 {workspace_base_dir}/{frontend}/{user}/ 与 /tmp/macchiato/{frontend}/{user}/
    下创建目录（幂等）。

    与 ensure_memory_owner_layout 的命名空间规则一致。
    """
    fe = validate_logic_namespace_segment(_ns_segment(source, "cli"), what="frontend")
    uid = validate_logic_namespace_segment(_ns_segment(user_id, "root"), what="user_id")
    owner = Path((cmd_cfg.workspace_base_dir or "./data/workspace").strip()) / fe / uid
    tmp_dir = _TMP_BASE_DIR / fe / uid
    created_paths: List[str] = []
    for path_obj, label in ((owner, "工作区路径"), (tmp_dir, "临时目录路径")):
        if path_obj.exists():
            if not path_obj.is_dir():
                raise ValueError(f"{label}已存在且不是目录: {path_obj}")
        else:
            path_obj.mkdir(parents=True, exist_ok=True)
            created_paths.append(str(path_obj))

    try:
        ensure_workspace_data_memory_symlink(
            owner, project_root=resolve_project_root(), source=source, user_id=uid
        )
    except Exception as exc:
        logger.warning("ensure_workspace_owner_layout: data/memory graft skipped: %s", exc)

    return {
        "frontend": fe,
        "user_id": uid,
        "workspace_owner": f"{fe}:{uid}",
        "owner_dir": str(owner),
        "tmp_dir": str(tmp_dir),
        "created_paths": created_paths,
    }


def list_user_ids_under_workspace(
    cmd_cfg: CommandToolsConfig, *, frontend: str = "cli"
) -> List[str]:
    """列出磁盘上某 frontend 下已有工作区目录的 user_id（仅一级子目录名）。"""
    fe = validate_logic_namespace_segment(_ns_segment(frontend, "cli"), what="frontend")
    base = Path((cmd_cfg.workspace_base_dir or "./data/workspace").strip()) / fe
    if not base.is_dir():
        return []
    names: List[str] = []
    for p in sorted(base.iterdir()):
        if p.is_dir() and not p.name.startswith("."):
            names.append(p.name)
    return names


def is_bash_workspace_admin(
    cmd_cfg: CommandToolsConfig,
    source: str,
    user_id: str,
    profile: Optional[Any] = None,
) -> bool:
    """
    是否对该 Core 关闭「按用户工作区隔离」并视为 bash 工作区管理员（cwd=base_dir，无 cd 笼）。

    优先级：CoreProfile.bash_workspace_admin → workspace_admin_memory_owners 列表。
    """
    if profile is not None and bool(getattr(profile, "bash_workspace_admin", False)):
        return True
    fe = validate_logic_namespace_segment(_ns_segment(source, "cli"), what="frontend")
    uid = validate_logic_namespace_segment(_ns_segment(user_id, "root"), what="user_id")
    mo = f"{fe}:{uid}"
    owners = {
        x.strip()
        for x in (cmd_cfg.workspace_admin_memory_owners or [])
        if x and x.strip()
    }
    return mo in owners


def resolve_bash_working_dir(
    cmd_cfg: CommandToolsConfig,
    user_id: str,
    *,
    source: str = "cli",
    profile: Optional[Any] = None,
) -> str:
    """
    解析 BashRuntime 应使用的初始 cwd。

    - 开启隔离且当前 Core 非管理员：{workspace_base_dir}/{frontend}/{user}/
    - 管理员或未开隔离：command_tools.base_dir（通常为项目根 ``.``）
    """
    if cmd_cfg.workspace_isolation_enabled and not is_bash_workspace_admin(
        cmd_cfg, source, user_id, profile
    ):
        layout = ensure_workspace_owner_layout(cmd_cfg, user_id, source=source)
        return layout["owner_dir"]
    return (cmd_cfg.base_dir or ".").strip() or "."


def build_bash_workspace_guard_init(
    workspace_root_resolved: str,
    *,
    project_root: Optional[str] = None,
    memory_long_term_dir: Optional[str] = None,
    memory_owner_dir: Optional[str] = None,
    real_home: Optional[str] = None,
) -> List[str]:
    """
    返回写入 bash 启动序列的脚本：覆盖 cd/pushd/popd；**HOME 即用户单元格根目录**
    （``MACCHIATO_WORKSPACE_ROOT``），不再嵌套 ``.sandbox_home``，避免「工作区里再套一层家目录」。

    并导出 ``MACCHIATO_PROJECT_ROOT``、``MACCHIATO_REAL_HOME``（进程级真实主目录）、记忆路径。

    在每次成功 cd 后用 pwd -P 校验是否仍位于 MACCHIATO_WORKSPACE_ROOT 之下。

    会话内 ``~`` / ``$HOME`` = 该用户数据根；需写**宿主机**用户主目录下路径时使用
    ``$MACCHIATO_REAL_HOME``（或经 ``request_permission`` 批准后的绝对路径）。
    """
    q = shlex.quote(str(Path(workspace_root_resolved).resolve()))
    pr = project_root or str(_PROJECT_ROOT.resolve())
    q_pr = shlex.quote(pr)
    q_real_home = shlex.quote(
        str(Path(real_home).resolve()) if real_home else str(Path.home().resolve())
    )
    extra_exports = ""
    if memory_long_term_dir:
        extra_exports += f'\nexport MACCHIATO_MEMORY_LONG_TERM={shlex.quote(memory_long_term_dir)}'
    if memory_owner_dir:
        extra_exports += f'\nexport MACCHIATO_MEMORY_OWNER_DIR={shlex.quote(memory_owner_dir)}'
    script = f"""
export MACCHIATO_REAL_HOME={q_real_home}
export MACCHIATO_WORKSPACE_ROOT={q}
export MACCHIATO_USER_ROOT="$MACCHIATO_WORKSPACE_ROOT"
export MACCHIATO_PROJECT_ROOT={q_pr}
mkdir -p "$MACCHIATO_WORKSPACE_ROOT" || true
export HOME="$MACCHIATO_WORKSPACE_ROOT"{extra_exports}
unset CDPATH
cd() {{
  builtin cd "$@" || return $?
  local here
  here=$(pwd -P)
  case "$here" in
    "$MACCHIATO_WORKSPACE_ROOT"|"$MACCHIATO_WORKSPACE_ROOT"/*) ;;
    *)
      echo "cd: 已阻止离开工作区 (macchiato)" >&2
      builtin cd "$MACCHIATO_WORKSPACE_ROOT" || true
      return 1
      ;;
  esac
}}
pushd() {{
  builtin pushd "$@" || return $?
  local here
  here=$(pwd -P)
  case "$here" in
    "$MACCHIATO_WORKSPACE_ROOT"|"$MACCHIATO_WORKSPACE_ROOT"/*) ;;
    *)
      echo "pushd: 已阻止离开工作区 (macchiato)" >&2
      builtin popd 2>/dev/null || true
      builtin cd "$MACCHIATO_WORKSPACE_ROOT" || true
      return 1
      ;;
  esac
}}
popd() {{
  builtin popd "$@" || return $?
  local here
  here=$(pwd -P)
  case "$here" in
    "$MACCHIATO_WORKSPACE_ROOT"|"$MACCHIATO_WORKSPACE_ROOT"/*) ;;
    *)
      echo "popd: 已阻止离开工作区 (macchiato)" >&2
      builtin cd "$MACCHIATO_WORKSPACE_ROOT" || true
      return 1
      ;;
  esac
}}
""".strip()
    return [script]


def remove_subagent_workspace_trees(
    cmd_cfg: CommandToolsConfig,
    sub_session_id: str,
) -> None:
    """
    删除 subagent 在隔离工作区下的目录（与 Bash 初始 cwd / ensure_workspace_owner_layout 一致）。

    - ``{workspace_base_dir}/subagent/{subagent_id}/``
    - ``/tmp/macchiato/subagent/{subagent_id}/``

    路径经 ``relative_to`` 校验，防止误删；目录不存在则跳过。
    供 CorePool.reap_zombie 在父会话拉取完整结果并收割 zombie 时调用。
    """
    sid = (sub_session_id or "").strip()
    if not sid.startswith("sub:"):
        return
    raw_id = sid[4:].strip()
    if not raw_id:
        return
    try:
        uid = validate_logic_namespace_segment(_ns_segment(raw_id, ""), what="subagent_id")
    except ValueError as exc:
        logger.warning(
            "remove_subagent_workspace_trees: invalid sub_session_id=%s: %s",
            sid,
            exc,
        )
        return

    base_data = Path((cmd_cfg.workspace_base_dir or "./data/workspace").strip()).resolve() / "subagent"
    owner_str = resolve_workspace_owner_dir(cmd_cfg, uid, source="subagent")
    owner_p = Path(owner_str).resolve()
    try:
        owner_p.relative_to(base_data)
    except ValueError:
        logger.warning(
            "remove_subagent_workspace_trees: refused path outside subagent workspace base owner=%s base=%s",
            owner_p,
            base_data,
        )
        return
    if owner_p.is_dir():
        shutil.rmtree(owner_p)
        logger.info("removed subagent workspace dir %s", owner_p)

    tmp_base = _TMP_BASE_DIR.resolve() / "subagent"
    tmp_str = resolve_workspace_tmp_dir(cmd_cfg, uid, source="subagent")
    tmp_p = Path(tmp_str).resolve()
    try:
        tmp_p.relative_to(tmp_base)
    except ValueError:
        logger.warning(
            "remove_subagent_workspace_trees: refused path outside tmp subagent base tmp=%s base=%s",
            tmp_p,
            tmp_base,
        )
        return
    if tmp_p.is_dir():
        shutil.rmtree(tmp_p)
        logger.info("removed subagent tmp workspace dir %s", tmp_p)
