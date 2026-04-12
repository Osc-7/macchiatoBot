"""
会话级路径语义（主进程内与 bash 对齐）。

工作区隔离且非 bash 工作区管理员时，``~`` / ``~/`` 表示 **当前逻辑用户的数据根**
（``{workspace_base_dir}/{frontend}/{user}/``），而非服务进程操作系统用户的 ``Path.home()``。

凡主进程解析含 ``~`` 的路径（file_tools、技能目录、ACL、媒体等）应通过本模块，
避免「工具写到 A、load_skill 读到 B」类分裂。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from agent_core.config import Config

from agent_core.agent.memory_paths import effective_memory_namespace_from_execution_context
from agent_core.agent.workspace_paths import (
    is_bash_workspace_admin,
    resolve_workspace_owner_dir,
)


def session_home_path(
    config: Config,
    *,
    source: str,
    user_id: str,
    profile: Optional[Any] = None,
    bash_workspace_admin: Optional[bool] = None,
) -> Path:
    """
    当前逻辑会话的「家目录」：隔离且非管理员时为工作区单元格；否则为 ``Path.home()``。
    """
    cmd = getattr(config, "command_tools", None)
    if cmd is None:
        return Path.home().resolve()
    use_cell = bool(getattr(cmd, "workspace_isolation_enabled", False))
    if bash_workspace_admin is not None:
        is_admin = bash_workspace_admin
    else:
        is_admin = is_bash_workspace_admin(cmd, source, user_id, profile)
    if not use_cell or is_admin:
        return Path.home().resolve()
    return Path(resolve_workspace_owner_dir(cmd, user_id, source=source)).resolve()


def session_home_path_from_exec_context(
    config: Config,
    exec_ctx: Optional[dict],
    *,
    profile: Optional[Any] = None,
) -> Path:
    """从 ``__execution_context__`` 解析会话家目录；无上下文时等价于 ``Path.home()``。"""
    ctx = dict(exec_ctx or {})
    src, uid = effective_memory_namespace_from_execution_context(ctx)
    return session_home_path(
        config,
        source=src,
        user_id=uid,
        profile=profile,
        bash_workspace_admin=ctx.get("bash_workspace_admin"),
    )


def session_home_path_from_agent(agent: Any) -> Path:
    """AgentCore 实例上取会话家目录（无 exec_ctx 时使用）。"""
    return session_home_path(
        agent._config,
        source=getattr(agent, "_source", "cli") or "cli",
        user_id=getattr(agent, "_user_id", "root") or "root",
        profile=getattr(agent, "_core_profile", None),
    )


def _use_isolated_tilde_semantics(
    config: Config,
    *,
    exec_ctx: Optional[dict],
    profile: Optional[Any],
) -> bool:
    ctx = dict(exec_ctx or {})
    src, uid = effective_memory_namespace_from_execution_context(ctx)
    cmd = getattr(config, "command_tools", None)
    if cmd is None:
        return False
    if not bool(getattr(cmd, "workspace_isolation_enabled", False)):
        return False
    bash_adm = ctx.get("bash_workspace_admin")
    if bash_adm is not None:
        return not bool(bash_adm)
    return not is_bash_workspace_admin(cmd, src, uid, profile)


def remap_tilde_path_str(
    path_str: str,
    *,
    session_home: Path,
    isolated_cell: bool,
) -> str:
    """
    将路径字符串中的 ``~`` / ``~/`` 按会话语义展开；``~user`` 等形式仍交 ``Path.expanduser``。
    """
    s = (path_str or "").strip()
    if not isolated_cell:
        return str(Path(s).expanduser())
    if s == "~":
        return str(session_home.resolve())
    if s.startswith("~/"):
        rest = s[2:].lstrip("/")
        if not rest:
            return str(session_home.resolve())
        return str((session_home / rest).resolve())
    if s.startswith("~") and len(s) > 1 and s[1] != "/":
        return str(Path(s).expanduser())
    return str(Path(s).expanduser())


def expand_user_path_str_for_session(
    path_str: str,
    config: Config,
    *,
    exec_ctx: Optional[dict] = None,
    profile: Optional[Any] = None,
) -> str:
    """
    展开路径字符串中的用户目录占位符，与当前会话（frontend/user/隔离策略）一致。

    供 file_tools、ACL、媒体等使用；非 ``~`` 前缀路径尽量保持原样再经 ``Path`` 处理。
    """
    ctx = dict(exec_ctx or {})
    src, uid = effective_memory_namespace_from_execution_context(ctx)
    home = session_home_path(
        config,
        source=src,
        user_id=uid,
        profile=profile,
        bash_workspace_admin=ctx.get("bash_workspace_admin"),
    )
    isolated = _use_isolated_tilde_semantics(config, exec_ctx=ctx, profile=profile)
    return remap_tilde_path_str(path_str, session_home=home, isolated_cell=isolated)
