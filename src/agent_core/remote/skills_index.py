"""Refresh and cache remote workspace skills index for system prompts."""

from __future__ import annotations

import json
import logging
import re
import shlex
from typing import Any, Dict, List, Optional, Tuple

import yaml

from agent_core.prompts.skills_roots import (
    SKILL_ROOT_RELS,
    format_skills_index_lines,
    remote_skill_rel_candidates,
)

logger = logging.getLogger(__name__)

_MAX_REMOTE_SKILLS = 64
_MAX_SKILL_MD_CHARS = 4000

# Runs on the remote worker inside the authorized workspace (HOME = workspace root).
# Prefer python3 -c (no bash heredoc): job runner wraps commands as `(cmd) > log`,
# and heredocs break if the closing delimiter shares a line with `)`.
_REMOTE_SKILLS_SCAN_SCRIPT = (
    "python3 -c "
    + shlex.quote(
        "\n".join(
            [
                "import json, pathlib",
                f"roots = {json.dumps(list(SKILL_ROOT_RELS), ensure_ascii=False)}",
                "seen = set()",
                "out = []",
                f"limit = {int(_MAX_REMOTE_SKILLS)}",
                "for rel in roots:",
                "    root = pathlib.Path(rel)",
                "    if not root.is_dir():",
                "        continue",
                "    for d in sorted(root.iterdir()):",
                "        if not d.is_dir() or d.name in seen:",
                "            continue",
                "        skill = d / 'SKILL.md'",
                "        if not skill.is_file():",
                "            continue",
                "        seen.add(d.name)",
                "        try:",
                "            text = skill.read_text(encoding='utf-8', errors='replace')",
                "        except OSError:",
                "            continue",
                "        out.append({'name': d.name, 'rel': str(skill.as_posix()), 'content': text[:8000]})",
                "        if len(out) >= limit:",
                "            break",
                "    if len(out) >= limit:",
                "        break",
                "print(json.dumps(out, ensure_ascii=False))",
            ]
        )
    )
)


def _parse_frontmatter(content: str) -> Tuple[Optional[str], Optional[str]]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content or "", re.DOTALL)
    if not match:
        return None, None
    try:
        meta = yaml.safe_load(match.group(1))
    except Exception:
        return None, None
    if not meta or not isinstance(meta, dict):
        return None, None
    name = meta.get("name")
    desc = meta.get("description")
    return (
        str(name).strip() if name else None,
        str(desc).strip() if desc else None,
    )


def build_skills_index_from_entries(
    entries: List[Dict[str, Any]],
    *,
    enabled: Optional[List[str]] = None,
    source_note: str = "",
) -> Tuple[str, List[str]]:
    """Build index markdown + name list from ``{name, content}`` entries."""
    enabled_set = {e.strip() for e in (enabled or []) if e and str(e).strip()}
    lines: List[str] = []
    names: List[str] = []
    seen: set[str] = set()
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        if not name or name in seen:
            continue
        if enabled_set and name not in enabled_set:
            continue
        seen.add(name)
        content = str(entry.get("content") or "")
        display, desc = _parse_frontmatter(content)
        display = display or name
        desc = desc or "(no description)"
        lines.append(f"- **{display}** (`{name}`): {desc}")
        names.append(name)
    index = format_skills_index_lines(
        lines,
        source_note=source_note
        or (
            "Skills are listed from the **current remote workspace** "
            "(`.macchiato/skills` then `.agents/skills`)."
        ),
    )
    return index, names


async def refresh_remote_workspace_skills_index(
    *,
    session_id: str,
    login: str,
    enabled: Optional[List[str]] = None,
    timeout_seconds: float = 45.0,
) -> Dict[str, Any]:
    """
    Scan remote workspace skill roots and cache the index on session state.

    Best-effort: failures clear/leave empty index and return ``ok=False``.
    """
    from agent_core.remote.worker_registry import get_remote_worker_registry
    from agent_core.remote.workspace_state import (
        get_remote_workspace_state,
        update_remote_workspace_skills_index,
    )

    sid = (session_id or "").strip()
    login_s = (login or "").strip()
    if not sid or not login_s:
        return {"ok": False, "error": "missing session_id or login", "names": []}

    state = get_remote_workspace_state(sid)
    if state is None:
        return {"ok": False, "error": "remote workspace not active", "names": []}

    try:
        result = await get_remote_worker_registry().execute_command(
            login=login_s,
            session_id=sid,
            command=_REMOTE_SKILLS_SCAN_SCRIPT.strip(),
            timeout_seconds=timeout_seconds,
            wait_for_completion=True,
            output_limit=512 * 1024,
        )
    except Exception as exc:
        logger.warning(
            "remote skills scan failed session=%s login=%s: %s", sid, login_s, exc
        )
        update_remote_workspace_skills_index(sid, index="", names=[])
        return {"ok": False, "error": str(exc), "names": []}

    if getattr(result, "error", None):
        update_remote_workspace_skills_index(sid, index="", names=[])
        return {
            "ok": False,
            "error": str(result.error),
            "names": [],
        }

    stdout = (getattr(result, "stdout", None) or "").strip()
    # Worker may wrap output; take last JSON array line.
    payload_text = stdout
    if stdout and not stdout.startswith("["):
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("["):
                payload_text = line
                break
    try:
        entries = json.loads(payload_text) if payload_text else []
        if not isinstance(entries, list):
            entries = []
    except json.JSONDecodeError:
        logger.warning(
            "remote skills scan returned non-JSON session=%s preview=%r",
            sid,
            stdout[:200],
        )
        update_remote_workspace_skills_index(sid, index="", names=[])
        return {"ok": False, "error": "invalid_scan_json", "names": []}

    index, names = build_skills_index_from_entries(entries, enabled=enabled)
    update_remote_workspace_skills_index(sid, index=index, names=names)
    return {"ok": True, "names": names, "count": len(names)}


async def load_remote_skill_markdown(
    *,
    session_id: str,
    login: str,
    skill_name: str,
    max_chars: int = _MAX_SKILL_MD_CHARS,
) -> Tuple[str, Optional[str], Dict[str, Any]]:
    """
    Read SKILL.md from remote workspace roots (``.macchiato`` then ``.agents``).

    Returns ``(content, error, metadata)``. Empty content + no error means not found.
    """
    from agent_core.remote.pathmap import normalize_remote_workspace_relative_path
    from agent_core.remote.worker_registry import get_remote_worker_registry

    metadata: Dict[str, Any] = {
        "workspace_backend": "remote",
        "remote_login": login,
    }
    candidates = remote_skill_rel_candidates(skill_name)
    if not candidates:
        return "", "skill_name cannot be empty", metadata

    for raw in candidates:
        rel, err = normalize_remote_workspace_relative_path(raw)
        if err or rel is None:
            continue
        metadata["remote_path"] = rel
        try:
            result = await get_remote_worker_registry().file_read(
                login=login,
                session_id=session_id,
                path=rel,
                encoding="utf-8",
            )
        except Exception as exc:
            return "", f"远程读取失败: {exc}", metadata

        if result.error:
            if result.error == "FILE_NOT_FOUND":
                continue
            return "", result.error, metadata

        content = (result.content or "").strip()
        if not content:
            continue
        if len(content) > max_chars:
            content = content[:max_chars].rstrip() + "\n\n<!-- 内容过长，已截断 -->"
        metadata["skill_root"] = rel.split("/")[0] if "/" in rel else rel
        return content, None, metadata

    # All candidates missing — soft not-found (no hard error).
    return "", None, metadata


async def refresh_remote_skills_best_effort(
    *,
    session_id: str,
    login: str,
    enabled: Optional[List[str]] = None,
) -> None:
    """Scan remote skill roots after workspace activation; never raise."""
    try:
        await refresh_remote_workspace_skills_index(
            session_id=session_id,
            login=login,
            enabled=enabled,
        )
    except Exception as exc:
        logger.warning(
            "remote skills index refresh skipped session=%s login=%s: %s",
            session_id,
            login,
            exc,
        )
