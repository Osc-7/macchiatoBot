"""In-process remote workspace state for AgentCore sessions."""

from __future__ import annotations

import threading
import time
from typing import Any, List, Optional

from macchiato_remote.protocol import (
    REMOTE_WORKSPACE_MOUNT,
    RemotePermissionProfile,
    RemoteWorkspaceState,
)

_STATE_BY_SESSION: dict[str, RemoteWorkspaceState] = {}
_LOCK = threading.RLock()
_DEFAULT_TTL_SECONDS = 2 * 60 * 60

# Cached progressive-disclosure skills index for remote sessions (daemon-side).
_SKILLS_INDEX_BY_SESSION: dict[str, dict[str, Any]] = {}


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
        # New activation invalidates any previous skills index for this session.
        _SKILLS_INDEX_BY_SESSION.pop(sid, None)
    return state


def get_remote_workspace_state(session_id: str) -> Optional[RemoteWorkspaceState]:
    sid = (session_id or "").strip()
    if not sid:
        return None
    with _LOCK:
        state = _STATE_BY_SESSION.get(sid)
        if state is not None and state.is_expired():
            _STATE_BY_SESSION.pop(sid, None)
            _SKILLS_INDEX_BY_SESSION.pop(sid, None)
            return None
        return state


def release_remote_workspace(session_id: str) -> Optional[RemoteWorkspaceState]:
    sid = (session_id or "").strip()
    if not sid:
        return None
    with _LOCK:
        _SKILLS_INDEX_BY_SESSION.pop(sid, None)
        return _STATE_BY_SESSION.pop(sid, None)


def clear_remote_workspace_state() -> None:
    """Clear all remote state; intended for tests."""
    with _LOCK:
        _STATE_BY_SESSION.clear()
        _SKILLS_INDEX_BY_SESSION.clear()


def update_remote_workspace_skills_index(
    session_id: str,
    *,
    index: str,
    names: Optional[List[str]] = None,
) -> None:
    """Cache the remote skills index markdown for prompt injection."""
    sid = (session_id or "").strip()
    if not sid:
        return
    with _LOCK:
        if sid not in _STATE_BY_SESSION:
            return
        _SKILLS_INDEX_BY_SESSION[sid] = {
            "index": (index or "").strip(),
            "names": list(names or []),
            "updated_at": time.time(),
        }


def get_remote_workspace_skills_index(session_id: str) -> str:
    """Return cached remote skills index markdown, or empty string."""
    sid = (session_id or "").strip()
    if not sid:
        return ""
    with _LOCK:
        state = _STATE_BY_SESSION.get(sid)
        if state is None or state.is_expired():
            return ""
        cached = _SKILLS_INDEX_BY_SESSION.get(sid) or {}
        return str(cached.get("index") or "").strip()


def format_remote_workspace_prompt_suffix(
    state: RemoteWorkspaceState,
) -> str:
    """Deprecated: remote mode is announced via conversation notices, not system.

    Kept as a no-op for callers/tests that still import the symbol.
    """
    _ = state
    return ""
