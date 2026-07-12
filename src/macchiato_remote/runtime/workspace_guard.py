"""Bash init snippets to keep remote shells inside the authorized workspace."""

from __future__ import annotations

import shlex
from pathlib import Path


def build_remote_workspace_guard_init(workspace_root: Path) -> str:
    """Inject cd/pushd/popd guard and MACCHIATO_* exports (aligned with local jail).

    Besides the primary workspace root, the daemon may set
    ``MACCHIATO_REMOTE_EXTRA_READ_ROOTS`` for a single command.  This is used
    after an explicit read grant for an absolute host path: cd/pushd/popd are
    still guarded, but they may enter those approved read roots as well.  The
    worker does not decide policy here; it only enforces the roots received from
    the daemon side.
    """
    q = shlex.quote(str(workspace_root.resolve()))
    return f"""
export MACCHIATO_WORKSPACE_ROOT={q}
export MACCHIATO_USER_ROOT={q}
export HOME={q}
export MACCHIATO_DIR={q}/.macchiato
export MACCHIATO_REMOTE=1
mkdir -p "$MACCHIATO_DIR" "$MACCHIATO_DIR/jobs" "$MACCHIATO_DIR/journal" "$MACCHIATO_DIR/rules" "$MACCHIATO_DIR/skills" "$MACCHIATO_DIR/scratch" || true
unset CDPATH
cd {q} 2>/dev/null || true
__macchiato_remote_path_allowed() {{
  local here="$1"
  case "$here" in
    {q}|{q}/*) return 0 ;;
  esac
  local roots="$MACCHIATO_REMOTE_EXTRA_READ_ROOTS"
  local old_ifs="$IFS"
  IFS=:
  for root in $roots; do
    [ -z "$root" ] && continue
    case "$here" in
      "$root"|"$root"/*)
        IFS="$old_ifs"
        return 0
        ;;
    esac
  done
  IFS="$old_ifs"
  return 1
}}
cd() {{
  builtin cd "$@" || return $?
  local here
  here=$(pwd -P)
  if __macchiato_remote_path_allowed "$here"; then
    return 0
  fi
  echo "cd: 已阻止离开远程工作区 (macchiato-remote)" >&2
  builtin cd {q} || true
  return 1
}}
pushd() {{
  builtin pushd "$@" || return $?
  local here
  here=$(pwd -P)
  if __macchiato_remote_path_allowed "$here"; then
    return 0
  fi
  echo "pushd: 已阻止离开远程工作区 (macchiato-remote)" >&2
  builtin popd 2>/dev/null || true
  builtin cd {q} || true
  return 1
}}
popd() {{
  builtin popd "$@" || return $?
  local here
  here=$(pwd -P)
  if __macchiato_remote_path_allowed "$here"; then
    return 0
  fi
  echo "popd: 已阻止离开远程工作区 (macchiato-remote)" >&2
  builtin cd {q} || true
  return 1
}}
""".strip()
