"""
Agent 进程表投影与可见性过滤。

数据源为 CorePool.list_entries(include_zombies=True)；不单独持久化。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Tuple

if TYPE_CHECKING:
    from system.kernel.core_pool import CoreEntry, CorePool


def memory_namespace_key(
    session_id: str,
    entry: "CoreEntry",
    pool: "CorePool",
) -> Tuple[str, str]:
    """沿 parent 链走到根会话，用根 CoreProfile 的 (frontend_id, dialog_window_id) 作为命名空间键。"""
    visited: set[str] = set()
    sid = session_id
    ent: Optional[CoreEntry] = entry
    while ent is not None and sid not in visited:
        visited.add(sid)
        parent = (ent.parent_session_id or "").strip()
        if not parent:
            break
        nxt = pool.get_entry(parent)
        if nxt is None:
            break
        sid = parent
        ent = nxt
    prof = ent.profile if ent is not None else entry.profile
    fe = (getattr(prof, "frontend_id", None) or "").strip()
    dw = (getattr(prof, "dialog_window_id", None) or "").strip()
    return fe, dw


def project_entry_row(
    session_id: str,
    entry: "CoreEntry",
    *,
    in_zombie_table: bool,
) -> Dict[str, Any]:
    """将 CoreEntry 投影为进程表一行（JSON 友好）。"""
    prof = entry.profile
    mode = getattr(prof, "mode", None) or ""
    kind = "sub" if session_id.startswith("sub:") else "main"
    if getattr(prof, "mode", None) == "sub":
        kind = "sub"

    if entry.sub_status:
        status = entry.sub_status
    elif entry.agent is not None:
        status = "active"
    else:
        status = "active"

    row: Dict[str, Any] = {
        "session_id": session_id,
        "parent_session_id": entry.parent_session_id,
        "kind": kind,
        "status": status,
        "profile_mode": mode,
        "in_zombie_table": in_zombie_table,
        "has_loaded_agent": entry.agent is not None,
    }
    if entry.task_description:
        desc = entry.task_description
        row["task_preview"] = (desc[:120] + "…") if len(desc) > 120 else desc
    return row


def filter_agent_rows(
    rows: List[Dict[str, Any]],
    *,
    scope: Literal["my_children", "namespace", "siblings"],
    caller_session_id: str,
    caller_entry: Optional["CoreEntry"],
    pool: "CorePool",
) -> List[Dict[str, Any]]:
    """按 scope 过滤进程表行。"""
    if caller_entry is None:
        return []

    if scope == "my_children":
        return [r for r in rows if r.get("parent_session_id") == caller_session_id]

    caller_ns = memory_namespace_key(caller_session_id, caller_entry, pool)

    if scope == "namespace":
        out: List[Dict[str, Any]] = []
        for r in rows:
            sid = str(r.get("session_id") or "")
            ent = pool.get_entry(sid)
            if ent is None:
                continue
            if memory_namespace_key(sid, ent, pool) == caller_ns:
                out.append(r)
        return out

    if scope == "siblings":
        parent = (caller_entry.parent_session_id or "").strip()
        if not parent:
            return []
        return [r for r in rows if r.get("parent_session_id") == parent]

    return []


def build_full_process_table(pool: "CorePool") -> List[Dict[str, Any]]:
    """列出 pool ∪ zombies 的全部投影行（不过滤）。"""
    rows: List[Dict[str, Any]] = []
    for sid, entry in pool.list_entries(include_zombies=True):
        rows.append(
            project_entry_row(
                sid, entry, in_zombie_table=pool.is_zombie(sid)
            )
        )
    return rows
