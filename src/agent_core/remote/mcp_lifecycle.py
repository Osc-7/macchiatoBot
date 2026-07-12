"""Shared hooks after remote workspace activation / release."""

from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


async def after_remote_workspace_activated(
    agent: Any,
    *,
    session_id: str,
) -> List[Any]:
    """Attach attach_on=remote_use MCP servers; return overlay rows."""
    try:
        from agent_core.mcp.session_overlay import get_mcp_session_overlay

        rows = await get_mcp_session_overlay().attach_defaults_for_remote_use(
            agent, session_id=session_id
        )
        return rows
    except Exception:
        logger.exception(
            "remote mcp attach_defaults failed session_id=%s", session_id
        )
        return []


async def before_remote_workspace_released(
    agent: Optional[Any],
    *,
    session_id: str,
) -> None:
    """Detach remote MCP tools for the session (best-effort)."""
    if agent is None:
        return
    try:
        from agent_core.mcp.session_overlay import get_mcp_session_overlay

        await get_mcp_session_overlay().detach_all_remote(
            agent, session_id=session_id
        )
    except Exception:
        logger.exception(
            "remote mcp detach_all failed session_id=%s", session_id
        )


async def release_remote_workspace_session(
    agent: Optional[Any],
    *,
    session_id: str,
) -> bool:
    """Detach MCP tools, clear daemon remote state, and close the worker session."""
    from agent_core.remote.workspace_state import (
        get_remote_workspace_state,
        release_remote_workspace,
    )
    from agent_core.remote.worker_registry import get_remote_worker_registry

    if get_remote_workspace_state(session_id) is None:
        return False

    await before_remote_workspace_released(agent, session_id=session_id)
    old = release_remote_workspace(session_id)
    if old is not None:
        try:
            await get_remote_worker_registry().close_workspace(
                login=old.login,
                session_id=session_id,
            )
        except Exception:
            logger.warning(
                "remote workspace close failed during session release "
                "session_id=%s login=%s",
                session_id,
                old.login,
                exc_info=True,
            )
    return old is not None


def format_remote_mcp_notice_line(rows: List[Any]) -> str:
    """Stable one-line summary for workspace notices."""
    if not rows:
        return ""
    ok_parts: List[str] = []
    err_parts: List[str] = []
    for row in rows:
        name = getattr(row, "name", "") or ""
        if getattr(row, "attached", False):
            n = len(getattr(row, "tool_names", None) or [])
            ok_parts.append(f"{name}({n} tools)")
        else:
            err = getattr(row, "error", None) or "failed"
            err_parts.append(f"{name}: {err}")
    bits: List[str] = []
    if ok_parts:
        bits.append(", ".join(ok_parts))
    if err_parts:
        bits.append("失败: " + ", ".join(err_parts))
    if not bits:
        return ""
    return "远程 MCP: " + " | ".join(bits)
