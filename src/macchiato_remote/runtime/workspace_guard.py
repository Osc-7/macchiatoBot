"""Bash init snippets to keep remote shells inside the authorized workspace."""

from __future__ import annotations

import shlex
from pathlib import Path


def build_remote_workspace_guard_init(workspace_root: Path) -> str:
    """Inject cd/pushd/popd guard and MACCHIATO_* exports (aligned with local jail)."""
    q = shlex.quote(str(workspace_root.resolve()))
    return f"""
export MACCHIATO_WORKSPACE_ROOT={q}
export MACCHIATO_USER_ROOT={q}
export HOME={q}
export MACCHIATO_REMOTE=1
unset CDPATH
cd {q} 2>/dev/null || true
cd() {{
  builtin cd "$@" || return $?
  local here
  here=$(pwd -P)
  case "$here" in
    {q}|{q}/*) ;;
    *)
      echo "cd: 已阻止离开远程工作区 (macchiato-remote)" >&2
      builtin cd {q} || true
      return 1
      ;;
  esac
}}
pushd() {{
  builtin pushd "$@" || return $?
  local here
  here=$(pwd -P)
  case "$here" in
    {q}|{q}/*) ;;
    *)
      echo "pushd: 已阻止离开远程工作区 (macchiato-remote)" >&2
      builtin popd 2>/dev/null || true
      builtin cd {q} || true
      return 1
      ;;
  esac
}}
popd() {{
  builtin popd "$@" || return $?
  local here
  here=$(pwd -P)
  case "$here" in
    {q}|{q}/*) ;;
    *)
      echo "popd: 已阻止离开远程工作区 (macchiato-remote)" >&2
      builtin cd {q} || true
      return 1
      ;;
  esac
}}
""".strip()
