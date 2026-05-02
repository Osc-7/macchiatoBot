"""统一的路径 grant 读写接口。

对上提供 read/write 两种 access_mode，屏蔽持久化文件名与进程内临时表的差异。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Literal, Optional, Tuple

from agent_core.agent.memory_paths import validate_logic_namespace_segment

if TYPE_CHECKING:
    from agent_core.config import Config

logger = logging.getLogger(__name__)

AccessMode = Literal["read", "write"]

_PERSISTED_FILE_BY_MODE: dict[AccessMode, str] = {
    "read": "readable_roots.json",
    "write": "writable_roots.json",
}
_EPHEMERAL_GRANTS: dict[AccessMode, Dict[Tuple[str, str], List[str]]] = {
    "read": {},
    "write": {},
}


def _normalize_mode(access_mode: str) -> AccessMode:
    mode = (access_mode or "").strip().lower()
    if mode not in ("read", "write"):
        raise ValueError(f"unsupported access_mode: {access_mode}")
    return mode  # type: ignore[return-value]


def _key(source: str, user_id: str) -> Tuple[str, str]:
    return ((source or "").strip() or "cli", (user_id or "").strip() or "root")


def _acl_path(
    acl_base_dir: str,
    source: str,
    user_id: str,
    *,
    access_mode: AccessMode,
) -> Path:
    fe = validate_logic_namespace_segment((source or "").strip() or "cli", what="frontend")
    uid = validate_logic_namespace_segment((user_id or "").strip() or "root", what="user_id")
    base = Path((acl_base_dir or "./data/acl").strip())
    return base / fe / uid / _PERSISTED_FILE_BY_MODE[access_mode]


def _normalize_prefix(
    prefix: str,
    *,
    source: str,
    user_id: str,
    config: Optional["Config"] = None,
) -> str:
    if config is not None:
        from agent_core.agent.session_paths import expand_user_path_str_for_session

        expanded = expand_user_path_str_for_session(
            prefix,
            config,
            exec_ctx={"source": source, "user_id": user_id},
        )
        return str(Path(expanded).resolve())
    return str(Path(prefix).expanduser().resolve())


def load_user_path_prefixes(
    acl_base_dir: str,
    source: str,
    user_id: str,
    *,
    access_mode: str,
) -> List[str]:
    """读取指定 access_mode 的持久 grant。"""
    mode = _normalize_mode(access_mode)
    path = _acl_path(acl_base_dir, source, user_id, access_mode=mode)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("%s grants read failed %s: %s", mode, path, exc)
        return []
    prefixes = raw.get("prefixes") if isinstance(raw, dict) else None
    if not isinstance(prefixes, list):
        return []
    out: List[str] = []
    for p in prefixes:
        if isinstance(p, str) and p.strip():
            try:
                out.append(str(Path(p).expanduser().resolve()))
            except OSError:
                continue
    return out


def append_user_path_prefix(
    acl_base_dir: str,
    source: str,
    user_id: str,
    prefix_abs: str,
    *,
    access_mode: str,
    config: Optional["Config"] = None,
) -> None:
    """幂等追加一条持久 grant。"""
    mode = _normalize_mode(access_mode)
    norm = _normalize_prefix(prefix_abs, source=source, user_id=user_id, config=config)
    existing = load_user_path_prefixes(
        acl_base_dir,
        source,
        user_id,
        access_mode=mode,
    )
    if norm in existing:
        return
    merged = sorted(set(existing + [norm]))
    path = _acl_path(acl_base_dir, source, user_id, access_mode=mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"prefixes": merged}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def add_ephemeral_path_prefix(
    source: str,
    user_id: str,
    prefix_abs: str,
    *,
    access_mode: str,
    config: Optional["Config"] = None,
) -> None:
    """为当前进程登记一条临时 grant。"""
    mode = _normalize_mode(access_mode)
    norm = _normalize_prefix(prefix_abs, source=source, user_id=user_id, config=config)
    k = _key(source, user_id)
    cur = _EPHEMERAL_GRANTS[mode].setdefault(k, [])
    if norm not in cur:
        cur.append(norm)
        logger.info(
            "ephemeral %s prefix added source=%s user=%s prefix=%s",
            mode,
            k[0],
            k[1],
            norm,
        )


def list_ephemeral_path_prefixes(
    source: str,
    user_id: str,
    *,
    access_mode: str,
) -> List[str]:
    mode = _normalize_mode(access_mode)
    return list(_EPHEMERAL_GRANTS[mode].get(_key(source, user_id), []))


def clear_ephemeral_path_grants_for_tests(*, access_mode: Optional[str] = None) -> None:
    """测试用：清空进程内临时 grant。"""
    if access_mode is None:
        for grants in _EPHEMERAL_GRANTS.values():
            grants.clear()
        return
    mode = _normalize_mode(access_mode)
    _EPHEMERAL_GRANTS[mode].clear()
