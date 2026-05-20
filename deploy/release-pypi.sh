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

echo ">>> Publishing to PyPI (one wheel at a time)..."
INDEX_URL="https://pypi.org/simple/"
FAILED=0
for whl in dist/*.whl; do
  echo "--- $whl"
  if uv publish --check-url "$INDEX_URL" "$whl"; then
    echo "OK: published or already identical on PyPI"
  else
    echo "SKIP/FAIL: $whl" >&2
    echo "  Common cause: this version is already on PyPI but your local wheel" >&2
    echo "  was rebuilt from newer commits (hash mismatch). Bump version in" >&2
    echo "  pyproject.toml and publish again; PyPI does not allow overwriting." >&2
    FAILED=1
  fi
done

echo ">>> Built wheels:"
ls -la dist/*.whl
exit "$FAILED"
