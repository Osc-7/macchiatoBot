#!/usr/bin/env bash
# Build and publish macchiato-bot + macchiato-remote to PyPI.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -z "${UV_PUBLISH_TOKEN:-}" ]]; then
  echo "Set UV_PUBLISH_TOKEN to your PyPI API token (pypi-...)." >&2
  exit 1
fi

rm -rf dist/
mkdir -p dist

echo ">>> Building wheels..."
uv build --wheel -o dist/
uv build --wheel -o dist/ packages/macchiato-remote

echo ">>> Publishing to PyPI..."
"${ROOT}/deploy/publish-dist.sh" dist
