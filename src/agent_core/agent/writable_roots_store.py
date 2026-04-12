"""每用户可写路径前缀持久化（供 bash/file 与 request_permission 批准后追加）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from agent_core.agent.memory_paths import validate_logic_namespace_segment

if TYPE_CHECKING:
    from agent_core.config import Config

logger = logging.getLogger(__name__)


def _acl_path(acl_base_dir: str, source: str, user_id: str) -> Path:
    fe = validate_logic_namespace_segment((source or "").strip() or "cli", what="frontend")
    uid = validate_logic_namespace_segment((user_id or "").strip() or "root", what="user_id")
    base = Path((acl_base_dir or "./data/acl").strip())
    return base / fe / uid / "writable_roots.json"


def load_user_writable_prefixes(
    acl_base_dir: str,
    source: str,
    user_id: str,
    config: Optional["Config"] = None,
) -> List[str]:
    """读取已规范化绝对路径前缀列表；文件不存在返回空列表。

    传入 ``config`` 时，条目中的 ``~`` 按当前 source/user 会话家目录展开（与 bash 一致）。
    """
    path = _acl_path(acl_base_dir, source, user_id)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("writable_roots read failed %s: %s", path, exc)
        return []
    prefixes = raw.get("prefixes") if isinstance(raw, dict) else None
    if not isinstance(prefixes, list):
        return []
    out: List[str] = []
    for p in prefixes:
        if isinstance(p, str) and p.strip():
            try:
                if config is not None:
                    from agent_core.agent.session_paths import (
                        expand_user_path_str_for_session,
                    )

                    exp = expand_user_path_str_for_session(
                        p.strip(),
                        config,
                        exec_ctx={"source": source, "user_id": user_id},
                    )
                    out.append(str(Path(exp).resolve()))
                else:
                    out.append(str(Path(p).expanduser().resolve()))
            except OSError:
                continue
    return out


def append_user_writable_prefix(
    acl_base_dir: str,
    source: str,
    user_id: str,
    prefix_abs: str,
    config: Optional["Config"] = None,
) -> None:
    """幂等追加一条绝对路径前缀并写回 JSON。"""
    if config is not None:
        from agent_core.agent.session_paths import expand_user_path_str_for_session

        norm = str(
            Path(
                expand_user_path_str_for_session(
                    prefix_abs,
                    config,
                    exec_ctx={"source": source, "user_id": user_id},
                )
            ).resolve()
        )
    else:
        norm = str(Path(prefix_abs).expanduser().resolve())
    existing = load_user_writable_prefixes(acl_base_dir, source, user_id, config=config)
    if norm in existing:
        return
    merged = sorted(set(existing + [norm]))
    path = _acl_path(acl_base_dir, source, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"prefixes": merged}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
