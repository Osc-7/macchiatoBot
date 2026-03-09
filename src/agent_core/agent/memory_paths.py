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


def resolve_memory_owner_paths(
    mem_cfg: MemoryConfig,
    user_id: str,
    config: Optional[Config] = None,
    source: str = "cli",
) -> Dict[str, str]:
    """
    计算记忆库路径，统一按 data/memory/{frontend}/{user}/ 划分。

    每个 owner 目录下包含：
    - content/      内容记忆（笔记、文档）
    - long_term/    长期记忆（entries.jsonl、MEMORY.md、markdown/）
    - chat_history.db  对话历史 SQLite

    示例：data/memory/cli/root/、data/memory/feishu/user123/、data/memory/shuiyuan/osc7/
    """
    base = Path((mem_cfg.memory_base_dir or "./data/memory").strip())
    frontend_id = _ns_segment(source, "cli")
    uid = _ns_segment(user_id, "root")

    if source == "shuiyuan" and config and getattr(config, "shuiyuan", None):
        shuiyuan_cfg = config.shuiyuan
        if shuiyuan_cfg.enabled and shuiyuan_cfg.memory:
            base = Path(shuiyuan_cfg.memory.long_term_dir).parent

    owner_dir = base / frontend_id / uid
    long_term_dir = owner_dir / "long_term"
    content_dir = owner_dir / "content"
    chat_db_path = owner_dir / "chat_history.db"
    memory_md_path = long_term_dir / "MEMORY.md"

    return {
        "long_term_dir": str(long_term_dir),
        "content_dir": str(content_dir),
        "chat_history_db_path": str(chat_db_path),
        "memory_md_path": str(memory_md_path),
    }


def new_session_id() -> str:
    return f"sess-{int(time.time())}-{uuid.uuid4().hex[:6]}"
