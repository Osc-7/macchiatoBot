"""进程内临时可读路径前缀（不写入 readable_roots.json）。

用于「批准本次」但不永久加入白名单；进程重启后失效。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from agent_core.config import Config

logger = logging.getLogger(__name__)

# (source, user_id) -> 规范化绝对路径前缀列表
_grants: Dict[Tuple[str, str], List[str]] = {}


def _key(source: str, user_id: str) -> Tuple[str, str]:
    return ((source or "").strip() or "cli", (user_id or "").strip() or "root")


def add_ephemeral_readable_prefix(
    source: str,
    user_id: str,
    prefix_abs: str,
    *,
    config: Optional["Config"] = None,
) -> None:
    """为当前进程登记一条可读前缀（幂等追加）。"""
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
    k = _key(source, user_id)
    cur = _grants.setdefault(k, [])
    if norm not in cur:
        cur.append(norm)
        logger.info(
            "ephemeral readable prefix added source=%s user=%s prefix=%s",
            k[0],
            k[1],
            norm,
        )


def list_ephemeral_readable_prefixes(source: str, user_id: str) -> List[str]:
    return list(_grants.get(_key(source, user_id), []))


def clear_ephemeral_readable_grants_for_tests() -> None:
    """测试用：清空进程内临时前缀。"""
    _grants.clear()
