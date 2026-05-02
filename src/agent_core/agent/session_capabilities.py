"""统一会话能力解析。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from agent_core.config import Config


@dataclass(frozen=True)
class SessionCapabilities:
    source: str
    user_id: str
    owner_root: Path
    session_home: Path
    tmp_root: Path
    project_root: Path
    initial_cwd: Path
    readable_roots: tuple[Path, ...]
    writable_roots: tuple[Path, ...]
    run_as_user: Optional[str]
    is_admin: bool
    workspace_isolated: bool
    uses_os_home: bool
    can_access_project_root: bool


def _dedupe_paths(paths: list[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return tuple(out)


def resolve_session_capabilities(
    config: Config,
    *,
    source: str,
    user_id: str,
    profile: Optional[Any] = None,
    bash_workspace_admin: Optional[bool] = None,
) -> SessionCapabilities:
    from agent_core.agent.memory_paths import resolve_memory_owner_paths
    from agent_core.agent.path_grants import (
        list_ephemeral_path_prefixes,
        load_user_path_prefixes,
    )
    from agent_core.agent.workspace_paths import (
        is_bash_workspace_admin,
        merged_bash_write_root_paths,
        resolve_project_root,
        resolve_workspace_owner_dir,
        resolve_workspace_tmp_dir,
    )
    from agent_core.bash_os_user import (
        logic_os_user_name,
        resolve_admin_system_user,
        resolve_os_user_home,
        should_use_os_home_for_logic_user,
    )

    cmd_cfg = config.command_tools
    workspace_isolated = bool(getattr(cmd_cfg, "workspace_isolation_enabled", False))
    is_admin = (
        bool(bash_workspace_admin)
        if bash_workspace_admin is not None
        else is_bash_workspace_admin(cmd_cfg, source, user_id, profile)
    )
    project_root = resolve_project_root().resolve()
    tmp_root = Path(
        resolve_workspace_tmp_dir(cmd_cfg, user_id, source=source)
    ).expanduser().resolve()

    run_as_user: Optional[str] = None
    if getattr(cmd_cfg, "bash_os_user_enabled", False):
        if is_admin:
            run_as_user = resolve_admin_system_user(cmd_cfg, source=source, user_id=user_id)
        elif workspace_isolated:
            run_as_user = logic_os_user_name(
                source,
                user_id,
                prefix=str(getattr(cmd_cfg, "bash_os_tenant_user_prefix", "m_")),
            )

    uses_os_home = (
        workspace_isolated
        and not is_admin
        and should_use_os_home_for_logic_user(
            cmd_cfg, source=source, user_id=user_id, profile=profile
        )
    )

    if run_as_user:
        owner_root = resolve_os_user_home(cmd_cfg, run_as_user)
    elif workspace_isolated:
        owner_root = Path(
            resolve_workspace_owner_dir(cmd_cfg, user_id, source=source)
        ).expanduser().resolve()
    else:
        owner_root = Path.home().resolve()

    owner_root = owner_root.resolve()
    session_home = owner_root
    initial_cwd = owner_root if workspace_isolated else Path(
        (cmd_cfg.base_dir or ".").strip() or "."
    ).expanduser().resolve()

    readable_roots: list[Path] = [owner_root, tmp_root]
    writable_roots: list[Path] = [owner_root, tmp_root]

    memory_cfg = getattr(config, "memory", None)
    if memory_cfg is not None and getattr(memory_cfg, "enabled", True):
        try:
            mem_paths = resolve_memory_owner_paths(
                memory_cfg, user_id, config=config, source=source
            )
            memory_root = Path(mem_paths["chat_history_db_path"]).parent.resolve()
            readable_roots.append(memory_root)
            writable_roots.append(memory_root)
        except Exception:
            pass

    extra_write_roots = merged_bash_write_root_paths(
        cmd_cfg,
        source,
        user_id,
        app_config=config,
    )
    writable_roots.extend(extra_write_roots)
    readable_roots.extend(extra_write_roots)

    readable_roots.extend(
        Path(p).resolve()
        for p in load_user_path_prefixes(
            cmd_cfg.acl_base_dir,
            source,
            user_id,
            access_mode="read",
        )
    )
    readable_roots.extend(
        Path(p).resolve()
        for p in list_ephemeral_path_prefixes(
            source,
            user_id,
            access_mode="read",
        )
    )

    can_access_project_root = bool(is_admin and workspace_isolated)
    if can_access_project_root:
        readable_roots.append(project_root)
        writable_roots.append(project_root)

    return SessionCapabilities(
        source=source,
        user_id=user_id,
        owner_root=owner_root,
        session_home=session_home,
        tmp_root=tmp_root,
        project_root=project_root,
        initial_cwd=initial_cwd,
        readable_roots=_dedupe_paths(readable_roots),
        writable_roots=_dedupe_paths(writable_roots),
        run_as_user=run_as_user,
        is_admin=is_admin,
        workspace_isolated=workspace_isolated,
        uses_os_home=uses_os_home,
        can_access_project_root=can_access_project_root,
    )


def resolve_session_capabilities_from_exec_ctx(
    config: Config,
    exec_ctx: Optional[dict],
    *,
    profile: Optional[Any] = None,
) -> SessionCapabilities:
    from agent_core.agent.memory_paths import (
        effective_memory_namespace_from_execution_context,
    )

    ctx = dict(exec_ctx or {})
    src, uid = effective_memory_namespace_from_execution_context(ctx)
    return resolve_session_capabilities(
        config,
        source=src,
        user_id=uid,
        profile=profile,
        bash_workspace_admin=ctx.get("bash_workspace_admin"),
    )


def resolve_session_capabilities_from_agent(agent: Any) -> SessionCapabilities:
    return resolve_session_capabilities(
        agent._config,
        source=getattr(agent, "_source", "cli") or "cli",
        user_id=getattr(agent, "_user_id", "root") or "root",
        profile=getattr(agent, "_core_profile", None),
    )
