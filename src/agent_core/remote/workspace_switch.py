"""Kernel-side admission and execution for ``switch_workspace``.

机器名、路径和在线状态都在调用时从 worker 注册表与本进程读取，
不写入提交进仓库的 prompt 文件。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent_core.tools.base import ToolResult

logger = logging.getLogger(__name__)

REMOTE_PROFILES = ("strict", "dev", "host-user", "host-admin")
_DAEMON_TARGETS = frozenset({"daemon", "cloud", "local"})
_agent_var: ContextVar[Any] = ContextVar("macchiato_kernel_agent", default=None)


def bind_kernel_agent(agent: Any) -> Any:
    """把当前 turn 的 AgentCore 交给工作区切换；返回 reset token。"""
    return _agent_var.set(agent)


def reset_kernel_agent(token: Any) -> None:
    _agent_var.reset(token)


def current_kernel_agent() -> Any:
    return _agent_var.get()


@dataclass
class WorkspaceAdmission:
    ok: bool
    error: str = ""
    message: str = ""
    target: str = ""
    path: str = ""
    profile: str = "dev"
    release: bool = False
    path_source: str = ""


def format_daemon_host_lines() -> str:
    """本进程所在机器的运行时描述。每次调用现算，不含写死的主机名。"""
    from agent_core.remote.workspace_notice import format_daemon_host_lines as _lines

    return _lines()


def kernel_admit_workspace_request(args: Dict[str, Any]) -> Optional[ToolResult]:
    """Kernel 入口：未登录、无连接或权限档位非法时直接拒绝，并把合法参数写回 args。"""
    admission = admit_workspace_request(
        target=args.get("target") or args.get("login"),
        path=args.get("path"),
        profile=args.get("profile"),
    )
    if not admission.ok:
        return ToolResult(
            success=False,
            data={
                "target": admission.target,
                "path": admission.path,
                "profile": admission.profile,
            },
            message=admission.message,
            error=admission.error or "WORKSPACE_DENIED",
        )
    args["target"] = admission.target
    args["path"] = admission.path
    args["profile"] = admission.profile
    return None


def admit_workspace_request(
    *,
    target: Any,
    path: Any = None,
    profile: Any = None,
) -> WorkspaceAdmission:
    """检查目标是否已登录、是否有活跃连接，以及请求的权限档位。"""
    raw_target = str(target or "").strip()
    if not raw_target:
        return WorkspaceAdmission(
            ok=False,
            error="MISSING_TARGET",
            message="要切换的目标不能为空。用 worker 的 login，或 target=daemon 回到 daemon。",
        )

    prof = str(profile or "").strip() or "dev"
    if prof not in REMOTE_PROFILES:
        return WorkspaceAdmission(
            ok=False,
            error="INVALID_PROFILE",
            target=raw_target,
            profile=prof,
            message=(
                f"权限档位 {prof!r} 无效。只能是 " + "、".join(REMOTE_PROFILES) + "。"
            ),
        )

    if raw_target.lower() in _DAEMON_TARGETS:
        return WorkspaceAdmission(
            ok=True,
            target="daemon",
            path="",
            profile=prof,
            release=True,
            path_source="daemon",
        )

    from agent_core.remote.worker_registry import get_remote_worker_registry

    registry = get_remote_worker_registry()
    connected = registry.is_connected(raw_target)
    registered = _login_is_registered(raw_target)
    if not registered and not connected:
        return WorkspaceAdmission(
            ok=False,
            error="NOT_LOGGED_IN",
            target=raw_target,
            profile=prof,
            message=(
                f"login {raw_target!r} 未在 daemon 登记。"
                "只能切换到运行环境列表里的机器。"
            ),
        )
    if not connected:
        return WorkspaceAdmission(
            ok=False,
            error="NOT_CONNECTED",
            target=raw_target,
            profile=prof,
            message=(
                f"login {raw_target!r} 已登录，但当前没有活跃连接。"
                "请在那台机器上启动 macchiato-remote 后再切换。"
            ),
        )

    default_path, default_source = _default_path_for_login(registry, raw_target)
    requested = str(path or "").strip()
    if requested and requested != default_path:
        return WorkspaceAdmission(
            ok=True,
            target=raw_target,
            path=requested,
            profile=prof,
            path_source="explicit",
        )
    return WorkspaceAdmission(
        ok=True,
        target=raw_target,
        path=default_path if not requested else requested,
        profile=prof,
        path_source=default_source,
    )


def _login_is_registered(login: str) -> bool:
    try:
        from system.automation.remote_worker_server import remote_server_token_map

        return login in remote_server_token_map()
    except Exception as exc:  # noqa: BLE001
        logger.debug("workspace switch: token map unavailable: %s", exc)
        return False


def _default_path_for_login(registry: Any, login: str) -> tuple[str, str]:
    """默认工作区是该 worker 启动 macchiato-remote 时的目录；旧 worker 未上报时用 ~。"""
    conn = registry._connections.get(login)
    meta = getattr(conn, "hello_meta", None) or {}
    reported = str(meta.get("default_workspace") or "").strip()
    if reported:
        return reported, "worker_start"
    return "~", "home"


def connected_worker_rows() -> List[Dict[str, str]]:
    from agent_core.remote.worker_registry import get_remote_worker_registry

    return get_remote_worker_registry().connected_snapshots()


def registered_logins() -> List[str]:
    try:
        from system.automation.remote_worker_server import remote_server_token_map

        return sorted(remote_server_token_map())
    except Exception as exc:  # noqa: BLE001
        logger.debug("workspace switch: cannot list logins: %s", exc)
        return []


def format_runtime_environment_section(agent: Any) -> str:
    """系统提示末尾的可变运行环境：daemon 事实 + 当前绑定 + 可切换机器。"""
    from agent_core.remote.workspace_state import get_remote_workspace_state

    session_id = str(getattr(agent, "_session_id", "") or "")
    remote = get_remote_workspace_state(session_id) if session_id else None
    lines: List[str] = [
        "# 运行环境",
        "",
        "以下由当前进程和已连接的 worker 现算，用来判断工具现在跑在哪一台机器上。",
        "",
        "## Daemon",
        format_daemon_host_lines(),
        "会话、记忆和 LLM 都在这台 daemon 上。远程工作区只改变 bash / 文件工具 / load_skill 的执行位置。",
    ]
    local_root = _session_daemon_workspace_root(agent)
    if local_root:
        lines.append(f"本会话在 daemon 上的工作区根: {local_root}")

    lines.extend(["", "## 当前工具执行位置"])
    if remote is None:
        lines.extend(
            [
                "后端: daemon",
                "bash / read_file / write_file / modify_file / load_skill 现在作用在上面这台 daemon 上，不是任何远程 worker。",
                "若上一轮刚从远程切回，以本节为准：工具已经回到 daemon。",
            ]
        )
    else:
        device = remote.device_label or remote.login
        lines.extend(
            [
                "后端: remote",
                f"远程登录: {remote.login}",
                f"远程机器: {device}",
                f"权限档位: {remote.profile}",
                f"授权目录: {remote.display_remote_path}",
                "bash / 文件工具 / load_skill 作用在这台远程机器上，不是 daemon 的磁盘。",
            ]
        )
        if _agent_may_switch_workspace(agent, session_id):
            lines.append("要回到 daemon，调用 switch_workspace，target 设为 daemon。")

    if not _agent_may_switch_workspace(agent, session_id):
        lines.extend(
            [
                "",
                "## 工作区切换",
                "当前会话无权切换工作区。不要调用 switch_workspace，也不要尝试 /remote-use 或 /remote-release。",
            ]
        )
        return "\n".join(lines).strip()

    lines.extend(["", "## 可切换机器"])
    online = connected_worker_rows()
    online_logins = {row["login"] for row in online}
    offline = [login for login in registered_logins() if login not in online_logins]
    if not online and not offline:
        lines.append("当前没有已登录或在线的远程 worker。工具只能在 daemon 上执行。")
    else:
        profiles = "、".join(REMOTE_PROFILES)
        if online:
            lines.append("在线（可切换）:")
            for row in online:
                default_path = row.get("default_workspace") or "~"
                device = row.get("device_label") or row["login"]
                os_name = row.get("os") or ""
                os_bit = f" 系统 {os_name}" if os_name else ""
                lines.append(
                    f"- login `{row['login']}` — 设备 {device}{os_bit}"
                    f" — 默认工作区 {default_path}（macchiato-remote 启动目录）"
                    f" — 可请求权限档位 {profiles}"
                )
        if offline:
            lines.append("已登录但未连接（现在不能切过去）:")
            for login in offline:
                lines.append(f"- login `{login}`")
    return "\n".join(lines).strip()


def _agent_may_switch_workspace(agent: Any, session_id: str) -> bool:
    from agent_core.kernel_interface.profile import may_switch_workspace

    profile = getattr(agent, "_core_profile", None) if agent is not None else None
    source = str(getattr(agent, "_source", "") or "") if agent is not None else ""
    return may_switch_workspace(profile, source=source, session_id=session_id)


def _workspace_switch_forbidden_result(agent: Any, session_id: str) -> Optional[ToolResult]:
    if _agent_may_switch_workspace(agent, session_id):
        return None
    return ToolResult(
        success=False,
        data={"session_id": session_id},
        message="当前会话无权切换工作区。水源、定时任务等 Core 不能使用 switch_workspace 或 /remote-use。",
        error="WORKSPACE_SWITCH_FORBIDDEN",
    )


def _session_daemon_workspace_root(agent: Any) -> str:
    config = getattr(agent, "_config", None)
    if config is None:
        return ""
    try:
        from agent_core.agent.session_capabilities import resolve_session_capabilities

        caps = resolve_session_capabilities(
            config,
            source=str(getattr(agent, "_source", "") or "cli"),
            user_id=str(getattr(agent, "_user_id", "") or "root"),
            profile=getattr(agent, "_core_profile", None),
        )
        return str(caps.initial_cwd)
    except Exception as exc:  # noqa: BLE001
        logger.debug("workspace switch: daemon workspace root unavailable: %s", exc)
        return ""


async def perform_workspace_switch(
    *,
    session_id: str,
    target: str,
    path: str = "",
    profile: str = "dev",
    ttl_seconds: Optional[int] = None,
    agent: Any = None,
) -> ToolResult:
    """在 Kernel 已放行的参数上切换当前会话的工作区。打开失败时保留原绑定。"""
    admission = admit_workspace_request(target=target, path=path, profile=profile)
    if not admission.ok:
        return ToolResult(
            success=False,
            data=None,
            message=admission.message,
            error=admission.error or "WORKSPACE_DENIED",
        )

    live = agent if agent is not None else current_kernel_agent()
    sid = (session_id or "").strip() or str(getattr(live, "_session_id", "") or "")
    if not sid:
        return ToolResult(
            success=False,
            data=None,
            message="当前会话 id 为空，无法切换工作区。",
            error="MISSING_SESSION",
        )
    denied = _workspace_switch_forbidden_result(live, sid)
    if denied is not None:
        return denied

    if admission.release:
        return await _release_to_daemon(sid, live)

    from agent_core.remote.worker_registry import get_remote_worker_registry
    from agent_core.remote.workspace_state import get_remote_workspace_state

    registry = get_remote_worker_registry()
    previous = get_remote_workspace_state(sid)
    try:
        opened = await registry.open_workspace(
            login=admission.target,
            session_id=sid,
            requested_path=admission.path,
            profile=admission.profile,  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "switch_workspace open failed session=%s login=%s: %s",
            sid,
            admission.target,
            exc,
        )
        return ToolResult(
            success=False,
            data={"target": admission.target, "kept_previous": previous is not None},
            message=f"未能打开 {admission.target} 的工作区，仍留在原处: {exc}",
            error="OPEN_FAILED",
        )

    if previous is not None:
        # 先卸掉旧工作区的远程 MCP。换 login 时再关掉旧 worker 会话；
        # 同一 login 只换目录时，open_workspace 已经换过 shell，不能再 close。
        await _retire_previous(
            sid,
            previous,
            live,
            registry,
            close_worker=previous.login != admission.target,
        )

    state = _activate_opened(
        sid,
        admission,
        opened,
        ttl_seconds=ttl_seconds,
    )
    mcp_line, skill_count = await _refresh_remote_side(sid, live, admission.target)
    notice = _remote_notice(state, skill_count=skill_count, mcp_line=mcp_line)
    _append_notice(live, notice)
    from agent_core.remote.workspace_notice import (
        format_frontend_remote_switch_message,
        notify_workspace_switch_frontend,
    )

    await notify_workspace_switch_frontend(format_frontend_remote_switch_message(state))
    data = state.model_dump()
    data["path_source"] = admission.path_source
    data["backend"] = "remote"
    return ToolResult(
        success=True,
        data=data,
        message=(
            f"已切换到 {admission.target}（{state.display_remote_path}，"
            f"权限 {admission.profile}）。接下来的 bash 和文件工具在这台远程机器上执行。"
        ),
    )


async def _release_to_daemon(session_id: str, agent: Any) -> ToolResult:
    from agent_core.remote.mcp_lifecycle import release_remote_workspace_session
    from agent_core.remote.workspace_notice import (
        format_frontend_daemon_switch_message,
        format_local_workspace_switch_notice,
        notify_workspace_switch_frontend,
    )
    from agent_core.remote.workspace_state import get_remote_workspace_state

    previous = get_remote_workspace_state(session_id)
    released = await release_remote_workspace_session(agent, session_id=session_id)
    notice = format_local_workspace_switch_notice(previous=previous)
    if previous is not None:
        _append_notice(agent, notice)
        workspace_dir = (
            _session_daemon_workspace_root(agent) if agent is not None else ""
        )
        await notify_workspace_switch_frontend(
            format_frontend_daemon_switch_message(
                previous=previous, workspace_dir=workspace_dir
            )
        )
    return ToolResult(
        success=True,
        data={
            "backend": "daemon",
            "target": "daemon",
            "released": released,
            "daemon": format_daemon_host_lines(),
        },
        message=(
            "已回到 daemon 所在机器。bash / 文件工具 / load_skill 现在作用在 daemon 上，"
            "不是远程 worker。\n" + format_daemon_host_lines()
        ),
    )


async def _retire_previous(
    session_id: str,
    previous: Any,
    agent: Any,
    registry: Any,
    *,
    close_worker: bool,
) -> None:
    """卸掉上一份远程工作区的 MCP。换机器时同时关闭旧 worker 会话。"""
    from agent_core.remote.mcp_lifecycle import before_remote_workspace_released
    from agent_core.remote.workspace_state import release_remote_workspace

    await before_remote_workspace_released(agent, session_id=session_id)
    if not close_worker:
        return
    release_remote_workspace(session_id)
    try:
        await registry.close_workspace(login=previous.login, session_id=session_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "switch_workspace close previous failed session=%s login=%s",
            session_id,
            previous.login,
            exc_info=True,
        )


def _activate_opened(
    session_id: str,
    admission: WorkspaceAdmission,
    opened: Any,
    *,
    ttl_seconds: Optional[int],
) -> Any:
    from agent_core.remote.workspace_state import activate_remote_workspace

    kwargs: Dict[str, Any] = {
        "session_id": session_id,
        "login": admission.target,
        "requested_path": admission.path,
        "profile": admission.profile,
        "resolved_path": getattr(opened, "resolved_path", None),
        "device_label": getattr(opened, "device_label", None),
    }
    if ttl_seconds is not None:
        kwargs["ttl_seconds"] = ttl_seconds
    return activate_remote_workspace(**kwargs)


async def _refresh_remote_side(
    session_id: str, agent: Any, login: str
) -> tuple[Optional[str], Optional[int]]:
    from agent_core.remote.mcp_lifecycle import (
        after_remote_workspace_activated,
        format_remote_mcp_notice_line,
    )
    from agent_core.remote.skills_index import refresh_remote_skills_best_effort
    from agent_core.remote.workspace_state import get_remote_workspace_skills_index

    enabled = None
    try:
        from agent_core.config import get_config

        enabled = list(getattr(get_config().skills, "enabled", None) or []) or None
    except Exception:  # noqa: BLE001
        enabled = None
    try:
        await refresh_remote_skills_best_effort(
            session_id=session_id, login=login, enabled=enabled
        )
    except Exception:  # noqa: BLE001
        logger.debug("switch_workspace skills refresh failed", exc_info=True)

    skill_count = None
    idx = get_remote_workspace_skills_index(session_id)
    if idx:
        skill_count = sum(
            1 for line in idx.splitlines() if line.strip().startswith("- **")
        )
    mcp_line = None
    if agent is not None:
        try:
            rows = await after_remote_workspace_activated(agent, session_id=session_id)
            mcp_line = format_remote_mcp_notice_line(rows) or None
        except Exception:  # noqa: BLE001
            logger.warning(
                "switch_workspace mcp attach failed session=%s",
                session_id,
                exc_info=True,
            )
    return mcp_line, skill_count


def _remote_notice(
    state: Any, *, skill_count: Optional[int], mcp_line: Optional[str]
) -> str:
    from agent_core.remote.workspace_notice import format_remote_workspace_switch_notice

    return format_remote_workspace_switch_notice(
        state,
        reason="activated",
        skill_count=skill_count,
        mcp_line=mcp_line,
    )


def _append_notice(agent: Any, notice: str) -> None:
    if agent is None:
        return
    try:
        from agent_core.remote.workspace_notice import append_workspace_switch_notice

        append_workspace_switch_notice(agent, notice, persist=True)
    except Exception:  # noqa: BLE001
        logger.warning("switch_workspace notice inject failed", exc_info=True)
