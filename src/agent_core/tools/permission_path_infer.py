"""从 request_permission 的 details 推断可读/可写 ACL 的路径前缀。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from agent_core.config import Config

logger = logging.getLogger(__name__)


def _infer_prefix_from_details(
    details: Any,
    *,
    config: "Config",
    exec_ctx: Dict[str, Any],
) -> Optional[str]:
    """
    从 details（字符串 JSON 或 dict）推断应写入 ACL 的绝对路径前缀。

    优先使用 ``path_prefix`` / ``writable_prefix``；否则从 ``path`` / ``target_path``
    推导（文件路径取父目录，目录路径规范化）。
    """
    if details is None:
        return None
    if isinstance(details, str):
        s = details.strip()
        if not s:
            return None
        try:
            d: Any = json.loads(s)
        except json.JSONDecodeError:
            return None
    elif isinstance(details, dict):
        d = details
    else:
        return None
    if not isinstance(d, dict):
        return None

    from agent_core.agent.session_paths import expand_user_path_str_for_session

    def _expand(p: str) -> str:
        return str(
            Path(
                expand_user_path_str_for_session(
                    p.strip(),
                    config,
                    exec_ctx=exec_ctx,
                )
            ).resolve()
        )

    for key in ("path_prefix", "writable_prefix"):
        raw = d.get(key)
        if raw is not None and str(raw).strip():
            try:
                return _expand(str(raw))
            except OSError as exc:
                logger.debug("permission infer: expand %s failed: %s", key, exc)
                return None

    for key in ("path", "target_path", "file"):
        raw = d.get(key)
        if raw is None or not str(raw).strip():
            continue
        s = str(raw).strip()
        try:
            if s.endswith("/") or s.endswith("\\"):
                return _expand(s)
            ppath = Path(
                expand_user_path_str_for_session(s, config, exec_ctx=exec_ctx)
            )
            if ppath.suffix:
                return str(ppath.parent.resolve())
            return str(ppath.resolve())
        except OSError as exc:
            logger.debug("permission infer: path %s failed: %s", key, exc)
            continue

    return None


def infer_writable_prefix_from_details(
    details: Any,
    *,
    config: "Config",
    exec_ctx: Dict[str, Any],
) -> Optional[str]:
    return _infer_prefix_from_details(details, config=config, exec_ctx=exec_ctx)


def infer_readable_prefix_from_details(
    details: Any,
    *,
    config: "Config",
    exec_ctx: Dict[str, Any],
) -> Optional[str]:
    return _infer_prefix_from_details(details, config=config, exec_ctx=exec_ctx)
