"""飞书会话重定向映射（持久化）。

用于让 ``/session new``、``/session switch``、``/new`` 在后续消息中持续生效，
避免下一条消息被默认的 ``map_event_to_session`` 覆盖回原会话。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

_OVERRIDE_FILE = "session_overrides.json"


def _base_dir() -> Path:
    test_dir = os.environ.get("SCHEDULE_AGENT_TEST_DATA_DIR")
    if test_dir:
        return Path(test_dir) / "feishu"
    return Path("data") / "feishu"


def _store_path() -> Path:
    return _base_dir() / _OVERRIDE_FILE


def _scope_key(*, chat_type: str, chat_id: str, open_id: str, user_id: str) -> str:
    chat_type_norm = (chat_type or "").strip() or "p2p"
    if chat_type_norm == "p2p":
        key = (open_id or "").strip() or (user_id or "").strip() or (chat_id or "").strip()
    else:
        key = (chat_id or "").strip() or (open_id or "").strip() or (user_id or "").strip()
    if not key:
        key = "unknown"
    return f"{chat_type_norm}:{key}"


def _load_all() -> Dict[str, str]:
    path = _store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def _save_all(mapping: Dict[str, str]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def set_session_override(
    *,
    chat_type: str,
    chat_id: str,
    open_id: str,
    user_id: str,
    session_id: str,
) -> None:
    sid = (session_id or "").strip()
    if not sid:
        return
    key = _scope_key(
        chat_type=chat_type,
        chat_id=chat_id,
        open_id=open_id,
        user_id=user_id,
    )
    mapping = _load_all()
    mapping[key] = sid
    _save_all(mapping)


def resolve_session_override(
    *,
    chat_type: str,
    chat_id: str,
    open_id: str,
    user_id: str,
) -> Optional[str]:
    key = _scope_key(
        chat_type=chat_type,
        chat_id=chat_id,
        open_id=open_id,
        user_id=user_id,
    )
    return _load_all().get(key)
