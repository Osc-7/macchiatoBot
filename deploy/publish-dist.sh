#!/usr/bin/env bash
# Publish wheels in dist/ to PyPI. Skip versions that already exist (no overwrite).
set -euo pipefail

if [[ -z "${UV_PUBLISH_TOKEN:-}" ]]; then
  echo "Set UV_PUBLISH_TOKEN (or PYPI_API_TOKEN in CI)." >&2
  exit 1
fi

DIST="${1:-dist}"
if [[ ! -d "$DIST" ]] || ! compgen -G "${DIST}/*.whl" >/dev/null; then
  echo "No wheels in ${DIST}/" >&2
  exit 1
fi

INDEX_URL="https://pypi.org/simple/"
FAILED=0

for whl in "${DIST}"/*.whl; do
  echo "--- $(basename "$whl")"
  base="$(basename "$whl")"
  pkg_ver="${base#macchiato_bot-}"
  pkg_ver="${pkg_ver#macchiato_remote-}"
  pkg_ver="${pkg_ver%-py3-none-any.whl}"
  if [[ "$base" == macchiato_bot-* ]]; then
    pypi_name="macchiato-bot"
  else
    pypi_name="macchiato-remote"
  fi
  if curl -fsS "https://pypi.org/pypi/${pypi_name}/${pkg_ver}/json" >/dev/null 2>&1; then
    echo "SKIP: ${pypi_name}==${pkg_ver} already on PyPI"
    continue
  fi
  if uv publish --check-url "$INDEX_URL" "$whl"; then
    echo "OK: published ${pypi_name}==${pkg_ver}"
  else
    echo "FAIL: $whl" >&2
    FAILED=1
  fi
done

exit "$FAILED"
