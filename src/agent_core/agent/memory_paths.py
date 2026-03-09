"""Helpers for agent memory path resolution and session identifiers."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Dict, Optional

from agent_core.config import Config, MemoryConfig


def _ns_segment(value: str, default: str) -> str:
    """规范化命名空间片段，空则用默认值。"""
    return (value or "").strip() or default


def _memory_namespace_dir(base_path: str, frontend_id: str, user_id: str) -> str:
    """按 frontend_id/user_id 划分的记忆目录，如 cli/root、feishu/root、shuiyuan/osc7。"""
    return str(Path(base_path) / frontend_id / user_id)


def resolve_memory_owner_paths(
    mem_cfg: MemoryConfig,
    user_id: str,
    config: Optional[Config] = None,
    source: str = "cli",
) -> Dict[str, str]:
    """
    Compute storage paths for all memory layers under the current owner.

    路径按 frontend_id/user_id 划分，实现多前端、多用户记忆隔离，例如：
    - cli/root
    - feishu/root
    - shuiyuan/osc7

    When `source=="shuiyuan"` and Shuiyuan memory is enabled, use shuiyuan config base
    but still apply frontend/user_id namespace (shuiyuan/osc7).
    """
    frontend_id = _ns_segment(source, "cli")
    uid = _ns_segment(user_id, "root")

    if source == "shuiyuan" and config and getattr(config, "shuiyuan", None):
        shuiyuan_cfg = config.shuiyuan
        if shuiyuan_cfg.enabled and shuiyuan_cfg.memory:
            mem = shuiyuan_cfg.memory
            long_term_base = Path(mem.long_term_dir)
            db_base = Path(shuiyuan_cfg.db_path).parent
            long_term_dir = str(long_term_base / uid)
            return {
                "short_term_dir": str(long_term_base.parent / "short_term" / "shuiyuan" / uid),
                "long_term_dir": long_term_dir,
                "content_dir": str(long_term_base.parent / "content" / "shuiyuan" / uid),
                "chat_history_db_path": str(db_base / "shuiyuan" / uid / "chat_history.db"),
                "memory_md_path": str(Path(long_term_dir) / "MEMORY.md"),
            }

    long_term_dir = _memory_namespace_dir(mem_cfg.long_term_dir, frontend_id, uid)
    short_term_dir = _memory_namespace_dir(mem_cfg.short_term_dir, frontend_id, uid)
    content_dir = _memory_namespace_dir(mem_cfg.content_dir, frontend_id, uid)
    db_parent = Path(mem_cfg.chat_history_db_path).parent
    chat_db_path = str(db_parent / frontend_id / uid / "chat_history.db")

    return {
        "short_term_dir": short_term_dir,
        "long_term_dir": long_term_dir,
        "content_dir": content_dir,
        "chat_history_db_path": chat_db_path,
        "memory_md_path": str(Path(long_term_dir) / "MEMORY.md"),
    }


def new_session_id() -> str:
    return f"sess-{int(time.time())}-{uuid.uuid4().hex[:6]}"
