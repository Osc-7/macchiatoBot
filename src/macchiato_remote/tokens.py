"""Token registry helpers for macchiato-remote workers."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

TOKEN_FILE_ENV = "MACCHIATO_REMOTE_TOKEN_FILE"
DEFAULT_TOKEN_REGISTRY_PATH = Path("data/automation/remote_worker_tokens.json")


def remote_token_registry_path(path: Optional[str | Path] = None) -> Path:
    raw = path or os.environ.get(TOKEN_FILE_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_TOKEN_REGISTRY_PATH


def token_digest(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def expected_token_matches(supplied_token: str, expected: str) -> bool:
    supplied = (supplied_token or "").strip()
    exp = (expected or "").strip()
    if not exp:
        return False
    if exp.startswith("sha256:"):
        return secrets.compare_digest(token_digest(supplied), exp)
    return secrets.compare_digest(supplied, exp)


def _read_registry(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "tokens": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "tokens": {}}
    if not isinstance(data, dict):
        return {"version": 1, "tokens": {}}
    if "tokens" not in data:
        return {"version": 1, "tokens": data}
    if not isinstance(data.get("tokens"), dict):
        data["tokens"] = {}
    return data


def _entry_expected_token(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if not isinstance(entry, dict):
        return ""
    digest = str(entry.get("token_sha256") or entry.get("sha256") or "").strip()
    if digest:
        return digest if digest.startswith("sha256:") else f"sha256:{digest}"
    return str(entry.get("token") or entry.get("value") or "").strip()


def load_registered_remote_worker_tokens(
    path: Optional[str | Path] = None,
) -> dict[str, str]:
    registry_path = remote_token_registry_path(path)
    data = _read_registry(registry_path)
    raw_tokens = data.get("tokens") if isinstance(data, dict) else {}
    if not isinstance(raw_tokens, dict):
        return {}

    out: dict[str, str] = {}
    for login, entry in raw_tokens.items():
        login_s = str(login).strip()
        expected = _entry_expected_token(entry)
        if login_s and expected:
            out[login_s] = expected
    return out


def register_remote_worker_token(
    login: str,
    token: str,
    path: Optional[str | Path] = None,
) -> Path:
    login_s = (login or "").strip()
    token_s = (token or "").strip()
    if not login_s:
        raise ValueError("login is required")
    if not token_s:
        raise ValueError("token is required")

    registry_path = remote_token_registry_path(path)
    data = _read_registry(registry_path)
    tokens = data.setdefault("tokens", {})
    if not isinstance(tokens, dict):
        tokens = {}
        data["tokens"] = tokens

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    old = tokens.get(login_s)
    created_at = old.get("created_at") if isinstance(old, dict) else None
    tokens[login_s] = {
        "token_sha256": token_digest(token_s),
        "created_at": created_at or now,
        "updated_at": now,
    }
    data["version"] = 1

    registry_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = registry_path.with_name(registry_path.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp_path, registry_path)
    try:
        registry_path.chmod(0o600)
    except OSError:
        pass
    return registry_path
