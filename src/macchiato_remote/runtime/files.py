"""Filesystem operations confined to an authorized workspace root."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple


def resolve_under_workspace(root: Path, relative: str) -> Path:
    rel = (relative or "").strip().replace("\\", "/").lstrip("/")
    if not rel or rel == ".":
        candidate = root.resolve()
    else:
        parts = Path(rel).parts
        if ".." in parts:
            raise ValueError("路径中不允许 ..")
        candidate = (root / rel).resolve()
    root_r = root.resolve()
    try:
        candidate.relative_to(root_r)
    except ValueError as exc:
        raise ValueError("路径越出授权工作区") from exc
    return candidate


def read_workspace_text(
    root: Path,
    relative: str,
    *,
    encoding: str = "utf-8",
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    max_chars: int = 2_000_000,
) -> Tuple[str, bool, Optional[str]]:
    """Returns (text, truncated, error_message)."""
    try:
        path = resolve_under_workspace(root, relative)
    except ValueError as exc:
        return "", False, str(exc)
    if not path.is_file():
        return "", False, "FILE_NOT_FOUND"
    try:
        raw = path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        return "", False, "ENCODING_ERROR"
    except OSError as exc:
        return "", False, str(exc)

    truncated = False
    if len(raw) > max_chars:
        raw = raw[:max_chars]
        truncated = True

    if start_line is not None or end_line is not None:
        lines = raw.splitlines(keepends=True)
        total = len(lines)
        start = 1 if start_line is None else max(1, int(start_line))
        end = total if end_line is None else int(end_line)
        if end < start:
            return "", truncated, "INVALID_LINE_RANGE"
        chunk = "".join(lines[start - 1 : end])
        return chunk, truncated, None

    return raw, truncated, None


def write_workspace_text(
    root: Path,
    relative: str,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: str = "overwrite",
) -> Tuple[int, Optional[str]]:
    """Returns (bytes_written, error_message)."""
    try:
        path = resolve_under_workspace(root, relative)
    except ValueError as exc:
        return 0, str(exc)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with open(path, "a", encoding=encoding) as handle:
                handle.write(content)
            return len(content.encode(encoding)), None
        path.write_text(content, encoding=encoding)
        return len(content.encode(encoding)), None
    except OSError as exc:
        return 0, str(exc)
