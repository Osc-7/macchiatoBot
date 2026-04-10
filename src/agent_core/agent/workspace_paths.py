"""Bash 工作区路径：按 frontend / user 划分，与 data/memory 布局一致。"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_core.config import CommandToolsConfig

from .memory_paths import validate_logic_namespace_segment


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


def ensure_workspace_owner_layout(
    cmd_cfg: CommandToolsConfig,
    user_id: str,
    *,
    source: str = "cli",
) -> Dict[str, Any]:
    """
    在 {workspace_base_dir}/{frontend}/{user}/ 下创建工作区目录（幂等）。

    与 ensure_memory_owner_layout 的命名空间规则一致。
    """
    fe = validate_logic_namespace_segment(_ns_segment(source, "cli"), what="frontend")
    uid = validate_logic_namespace_segment(_ns_segment(user_id, "root"), what="user_id")
    owner = Path((cmd_cfg.workspace_base_dir or "./data/workspace").strip()) / fe / uid
    created_paths: List[str] = []
    if owner.exists():
        if not owner.is_dir():
            raise ValueError(f"工作区路径已存在且不是目录: {owner}")
    else:
        owner.mkdir(parents=True, exist_ok=True)
        created_paths.append(str(owner))
    return {
        "frontend": fe,
        "user_id": uid,
        "workspace_owner": f"{fe}:{uid}",
        "owner_dir": str(owner),
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


def build_bash_workspace_guard_init(workspace_root_resolved: str) -> List[str]:
    """
    返回写入 bash 启动序列的脚本：覆盖 cd/pushd/popd，并把 HOME 指到工作区。

    在每次成功 cd 后用 pwd -P 校验是否仍位于 MACCHIATO_WORKSPACE_ROOT 之下；
    无法兜住「直接对文件使用绝对路径读写」等情形，另由工具层与运维策略约束。
    """
    q = shlex.quote(str(Path(workspace_root_resolved).resolve()))
    script = f"""
export MACCHIATO_WORKSPACE_ROOT={q}
export HOME="$MACCHIATO_WORKSPACE_ROOT"
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
