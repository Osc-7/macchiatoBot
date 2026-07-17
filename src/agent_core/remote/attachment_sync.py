"""Mirror daemon-local upload attachments into an active remote workspace inbox."""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from macchiato_remote.protocol import REMOTE_BLOB_MAX_BYTES
from macchiato_remote.runtime.macchiato_dir import INBOX_REL

logger = logging.getLogger(__name__)

_UPGRADE_HINT = (
    "附件未同步到远程工作区：请升级 macchiato-remote≥0.2.9（需 file_blob_write）"
)
_SYNC_HINT = "已同步到远程工作区"


def _safe_inbox_filename(name: str, *, fallback: str = "attachment.bin") -> str:
    base = Path((name or "").strip()).name or fallback
    cleaned = "".join(c if (c.isalnum() or c in "._-+") else "_" for c in base)
    cleaned = cleaned.strip("._") or fallback
    if cleaned in {".", ".."}:
        cleaned = fallback
    return cleaned[:180]


def _unique_inbox_rel(name: str, used: set[str]) -> str:
    safe = _safe_inbox_filename(name)
    candidate = f"{INBOX_REL}/{safe}"
    if candidate not in used:
        used.add(candidate)
        return candidate
    stem = Path(safe).stem
    suffix = Path(safe).suffix
    idx = 2
    while True:
        alt = f"{INBOX_REL}/{stem}_{idx}{suffix}"
        if alt not in used:
            used.add(alt)
            return alt
        idx += 1


def _display_path_for_item(item: Dict[str, Any]) -> str:
    remote = str(item.get("remote_path") or "").strip()
    if remote:
        return remote
    return str(item.get("path") or "").strip()


def _rewrite_text_paths(text: str, replacements: Dict[str, str]) -> str:
    if not text or not replacements:
        return text
    out = text
    # Longer paths first to avoid partial overlaps.
    for local, remote in sorted(replacements.items(), key=lambda kv: -len(kv[0])):
        if local and remote and local in out:
            out = out.replace(local, remote)
    return out


def worker_supports_file_blob_write(
    *,
    protocol_version: Optional[int],
    capabilities: Optional[List[str]],
) -> bool:
    if protocol_version is not None and int(protocol_version) < 4:
        # Still allow if capability is present (forward-compat).
        caps = set(capabilities or [])
        return "file_blob_write" in caps
    caps = set(capabilities or [])
    return "file_blob_write" in caps


async def sync_content_items_to_remote_inbox(
    *,
    session_id: str,
    content_items: Optional[List[Dict[str, Any]]],
    user_text: str = "",
    max_bytes: int = REMOTE_BLOB_MAX_BYTES,
) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    """
    When a remote workspace is active, copy local attachment files to
    ``.macchiato/inbox/`` and expose remote relative paths in user-facing text.

    ``path`` on media items stays as the daemon-local absolute path for vision
    hydrate; ``remote_path`` is set for tool/read prompts.

    Returns ``(items, user_text, notices)``. Notices are soft warnings and do not
    block the turn.
    """
    items = [dict(x) if isinstance(x, dict) else x for x in (content_items or [])]
    text = user_text or ""
    notices: List[str] = []
    sid = (session_id or "").strip()
    if not sid or not items:
        return items, text, notices

    try:
        from agent_core.remote.worker_registry import get_remote_worker_registry
        from agent_core.remote.workspace_state import get_remote_workspace_state
    except Exception:
        return items, text, notices

    state = get_remote_workspace_state(sid)
    if state is None:
        return items, text, notices

    registry = get_remote_worker_registry()
    conn = await registry.get(state.login)
    if conn is None:
        notices.append("附件未同步到远程工作区：远程 worker 未连接")
        return items, text, notices

    hello = conn.hello_meta or {}
    caps = list(hello.get("capabilities") or [])
    proto = hello.get("protocol_version")
    try:
        proto_i = int(proto) if proto is not None else None
    except (TypeError, ValueError):
        proto_i = None
    if not worker_supports_file_blob_write(
        protocol_version=proto_i, capabilities=caps
    ):
        notices.append(_UPGRADE_HINT)
        return items, text, notices

    used_names: set[str] = set()
    replacements: Dict[str, str] = {}
    synced_any = False

    for item in items:
        if not isinstance(item, dict):
            continue
        local_path = str(item.get("path") or "").strip()
        if not local_path:
            # Text-only items may still embed a path; handled via replacements later.
            continue
        path_obj = Path(local_path)
        if not path_obj.is_file():
            continue

        try:
            size = path_obj.stat().st_size
        except OSError as exc:
            notices.append(f"附件跳过（无法读取）: {path_obj.name}: {exc}")
            continue
        if size > max_bytes:
            notices.append(
                f"附件过大未同步到远程（>{max_bytes} bytes）: {path_obj.name}"
            )
            continue

        try:
            raw = path_obj.read_bytes()
        except OSError as exc:
            notices.append(f"附件跳过（读取失败）: {path_obj.name}: {exc}")
            continue

        preferred = str(item.get("name") or "").strip() or path_obj.name
        remote_rel = _unique_inbox_rel(preferred, used_names)
        b64 = base64.b64encode(raw).decode("ascii")
        try:
            result = await registry.file_blob_write(
                login=state.login,
                session_id=sid,
                path=remote_rel,
                content_base64=b64,
                mode="overwrite",
                max_bytes=max_bytes,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("attachment sync failed path=%s: %s", local_path, exc)
            notices.append(f"附件同步失败: {path_obj.name}: {exc}")
            continue
        if result.error:
            notices.append(f"附件同步失败: {path_obj.name}: {result.error}")
            continue

        item["remote_path"] = remote_rel
        replacements[local_path] = remote_rel
        synced_any = True

    # Rewrite embedded local paths in text-typed content items.
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            item["text"] = _rewrite_text_paths(item["text"], replacements)

    text = _rewrite_text_paths(text, replacements)
    if synced_any:
        notices.append(_SYNC_HINT)

    return items, text, notices


def format_attachment_sync_notices(notices: List[str]) -> str:
    """Format soft notices for appending to user text."""
    cleaned = [str(n).strip() for n in notices if str(n).strip()]
    if not cleaned:
        return ""
    # Drop pure success hint if it's the only line — media_helpers already
    # mentions remote paths; keep upgrade/error notices always.
    if cleaned == [_SYNC_HINT]:
        return f"[{_SYNC_HINT}]"
    lines = []
    for n in cleaned:
        if n == _SYNC_HINT:
            lines.append(f"[{n}]")
        else:
            lines.append(f"[远程附件] {n}")
    return "\n".join(lines)


# Re-export helper used by media_helpers tests / adapters.
__all__ = [
    "format_attachment_sync_notices",
    "sync_content_items_to_remote_inbox",
    "worker_supports_file_blob_write",
    "_display_path_for_item",
]
