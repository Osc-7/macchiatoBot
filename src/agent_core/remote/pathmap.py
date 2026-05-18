"""Map agent-facing paths to remote workspace-relative paths."""

from __future__ import annotations

from pathlib import PurePosixPath

from macchiato_remote.protocol import REMOTE_WORKSPACE_MOUNT


def normalize_remote_workspace_relative_path(
    path_str: str,
) -> tuple[str | None, str | None]:
    """Return (relative_posix_path, error_message).

    Accepts:
    - Relative paths (``README.md``, ``src/a.py``)
    - ``~/foo`` → ``foo`` under remote root
    - ``/workspace/...`` logical mount
    - Absolute paths (``/foo/bar``) as real absolute paths on remote host
    """
    raw = (path_str or "").strip()
    if not raw:
        return None, "路径不能为空"

    mount = REMOTE_WORKSPACE_MOUNT.rstrip("/") or "/workspace"
    if raw.startswith(mount + "/"):
        rel = raw[len(mount) + 1 :]
    elif raw == mount:
        rel = "."
    elif raw.startswith("~/"):
        rel = raw[2:].lstrip("/")
    elif raw.startswith("/"):
        rel = raw
    else:
        rel = raw.lstrip("/")

    if not rel:
        rel = "."

    pp = PurePosixPath(rel)
    if any(p == ".." for p in pp.parts):
        return None, "路径中不允许 .."

    return str(pp), None
