#!/usr/bin/env bash
# Install macchiato-remote worker CLI (lightweight; no full macchiato-bot).
set -euo pipefail

INSTALLER="${INSTALLER:-uv}"
VERSION="${MACCHIATO_REMOTE_VERSION:-}"

usage() {
  cat <<'EOF'
Usage: install-macchiato-remote.sh

Environment:
  INSTALLER=uv|pipx|pip   (default: uv)
  MACCHIATO_REMOTE_VERSION=0.2.1   optional pin

Examples:
  curl -fsSL .../deploy/install-macchiato-remote.sh | bash
  INSTALLER=pipx MACCHIATO_REMOTE_VERSION=0.2.1 ./deploy/install-macchiato-remote.sh
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

spec="macchiato-remote"
if [[ -n "${VERSION}" ]]; then
  spec="macchiato-remote==${VERSION}"
fi

case "${INSTALLER}" in
  uv)
    if ! command -v uv >/dev/null 2>&1; then
      echo "uv not found; install from https://docs.astral.sh/uv/ or set INSTALLER=pipx" >&2
      exit 1
    fi
    uv tool install "${spec}" --force
    ;;
  pipx)
    if ! command -v pipx >/dev/null 2>&1; then
      echo "pipx not found" >&2
      exit 1
    fi
    pipx install "${spec}" --force
    ;;
  pip)
    if ! command -v pip >/dev/null 2>&1; then
      echo "pip not found" >&2
      exit 1
    fi
    pip install --user "${spec}"
    ;;
  *)
    echo "Unknown INSTALLER=${INSTALLER}" >&2
    exit 1
    ;;
esac

echo "Installed. Run: macchiato-remote --version"
