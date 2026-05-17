"""In-process remote workspace state for AgentCore sessions."""

from __future__ import annotations

import threading
import time
from typing import Optional

from macchiato_remote.protocol import (
    REMOTE_WORKSPACE_MOUNT,
    RemotePermissionProfile,
    RemoteWorkspaceState,
)

_STATE_BY_SESSION: dict[str, RemoteWorkspaceState] = {}
_LOCK = threading.RLock()
_DEFAULT_TTL_SECONDS = 2 * 60 * 60


def activate_remote_workspace(
    *,
    session_id: str,
    login: str,
    requested_path: str,
    profile: RemotePermissionProfile = "dev",
    ttl_seconds: Optional[int] = _DEFAULT_TTL_SECONDS,
    resolved_path: Optional[str] = None,
    device_label: Optional[str] = None,
) -> RemoteWorkspaceState:
    """Mark a session as using a remote workspace backend.

    The first slice records state and updates prompting. Actual worker routing
    will consume this same state in the remote runtime/file adapter layer.
    """
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id 不能为空")
    login_s = (login or "").strip()
    if not login_s:
        raise ValueError("login 不能为空")
    path_s = (requested_path or "").strip() or "~"
    expires_at = None
    if ttl_seconds is not None and int(ttl_seconds) > 0:
        expires_at = time.time() + int(ttl_seconds)
    state = RemoteWorkspaceState(
        session_id=sid,
        login=login_s,
        requested_path=path_s,
        resolved_path=(resolved_path or "").strip() or None,
        profile=profile,
        status="active",
        workspace_mount=REMOTE_WORKSPACE_MOUNT,
        device_label=(device_label or "").strip() or None,
        expires_at=expires_at,
    )
    with _LOCK:
        _STATE_BY_SESSION[sid] = state
    return state


def get_remote_workspace_state(session_id: str) -> Optional[RemoteWorkspaceState]:
    sid = (session_id or "").strip()
    if not sid:
        return None
    with _LOCK:
        state = _STATE_BY_SESSION.get(sid)
        if state is not None and state.is_expired():
            _STATE_BY_SESSION.pop(sid, None)
            return None
        return state


def release_remote_workspace(session_id: str) -> Optional[RemoteWorkspaceState]:
    sid = (session_id or "").strip()
    if not sid:
        return None
    with _LOCK:
        return _STATE_BY_SESSION.pop(sid, None)


def clear_remote_workspace_state() -> None:
    """Clear all remote state; intended for tests."""
    with _LOCK:
        _STATE_BY_SESSION.clear()


def format_remote_workspace_prompt_suffix(
    state: RemoteWorkspaceState,
) -> str:
    """Build the system prompt suffix used only while remote mode is active."""
    device = state.device_label or state.login
    ttl_line = ""
    if state.expires_at is not None:
        remaining = max(0, int(state.expires_at - time.time()))
        ttl_line = f"\n租约剩余: 约 {remaining // 60} 分钟"
    return f"""
# 当前远程工作区模式

本会话的 bash、文件工具与 load_skill 当前运行在用户授权的远程机器上，而不是云服务器。
远程登录: {state.login}
远程机器: {device}
权限档位: {state.profile}
当前工作区: {state.workspace_mount}，对应远程机器授权目录: {state.display_remote_path}{ttl_line}

请像操作普通工作区一样使用 bash、read_file、write_file、modify_file。
相对路径、~、{state.workspace_mount} 都指向远程工作区。不要假设云服务器项目目录对当前 bash 可见。
如需访问工作区外路径，必须请求用户授权或提示用户使用 /remote-grant、/remote-elevate。
""".strip()
