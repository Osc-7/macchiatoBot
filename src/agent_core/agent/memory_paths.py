"""Helpers for agent memory path resolution and session identifiers."""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_core.config import Config, MemoryConfig

_NS_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _ns_segment(value: str, default: str) -> str:
    """规范化命名空间片段，空则用默认值。"""
    return (value or "").strip() or default


def validate_logic_namespace_segment(value: str, *, what: str = "segment") -> str:
    """
    校验多前端/多用户下的逻辑命名片段（frontend、user_id 等）。

    禁止路径穿越与冒号（session_id / memory_owner 用冒号拼接）。
    """
    s = (value or "").strip()
    if not s:
        raise ValueError(f"{what} 不能为空")
    if len(s) > 128:
        raise ValueError(f"{what} 过长（最多 128 字符）")
    if s in (".", "..") or any(c in s for c in "/\\:"):
        raise ValueError(f"{what} 不能包含路径分隔符或冒号")
    if not _NS_SEGMENT_RE.fullmatch(s):
        raise ValueError(f"{what} 仅允许字母、数字、._-")
    return s


def ensure_memory_owner_layout(
    mem_cfg: MemoryConfig,
    user_id: str,
    *,
    source: str = "cli",
) -> Dict[str, Any]:
    """
    在 data/memory/{frontend}/{user}/ 下创建标准目录布局（幂等）。

    返回 memory_owner、default_session_id、已新建的路径列表等。
    """
    fe = validate_logic_namespace_segment(_ns_segment(source, "cli"), what="frontend")
    uid = validate_logic_namespace_segment(_ns_segment(user_id, "root"), what="user_id")
    paths = resolve_memory_owner_paths(mem_cfg, uid, source=fe)
    owner = Path(paths["chat_history_db_path"]).parent
    long_term = Path(paths["long_term_dir"])
    content = Path(paths["content_dir"])
    created_paths: List[str] = []
    for p in (owner, long_term, content):
        if p.exists():
            if not p.is_dir():
                raise ValueError(f"路径已存在且不是目录: {p}")
            continue
        p.mkdir(parents=True, exist_ok=True)
        created_paths.append(str(p))
    return {
        "frontend": fe,
        "user_id": uid,
        "memory_owner": f"{fe}:{uid}",
        "default_session_id": f"{fe}:{uid}",
        "owner_dir": str(owner),
        "paths": paths,
        "created_paths": created_paths,
    }


def list_user_ids_under_frontend(mem_cfg: MemoryConfig, *, frontend: str = "cli") -> List[str]:
    """列出磁盘上某 frontend 下已有记忆目录的 user_id（仅一级子目录名）。"""
    fe = validate_logic_namespace_segment(_ns_segment(frontend, "cli"), what="frontend")
    base = Path((mem_cfg.memory_base_dir or "./data/memory").strip()) / fe
    if not base.is_dir():
        return []
    names: List[str] = []
    for p in sorted(base.iterdir()):
        if p.is_dir() and not p.name.startswith("."):
            names.append(p.name)
    return names


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

    # 统一按 data/memory/{frontend}/{user}/ 划分，不使用 data/memory/long_term/shuiyuan
    owner_dir = base / frontend_id / uid
    long_term_dir = owner_dir / "long_term"
    content_dir = owner_dir / "content"
    chat_db_path = owner_dir / "chat_history.db"
    memory_md_path = long_term_dir / "MEMORY.md"

    checkpoint_path = owner_dir / "checkpoint.json"

    return {
        "long_term_dir": str(long_term_dir),
        "content_dir": str(content_dir),
        "chat_history_db_path": str(chat_db_path),
        "memory_md_path": str(memory_md_path),
        "checkpoint_path": str(checkpoint_path),
    }


def get_kernel_shutdown_at_path(mem_cfg: MemoryConfig) -> str:
    """
    Kernel 关闭时写入的时间戳文件路径（进程级单例）。

    用于下次启动时计算 elapsed = shutdown_at - checkpoint.last_active_at，
    判断 core 是否已过期并正确恢复剩余 TTL。
    """
    base = Path((mem_cfg.memory_base_dir or "./data/memory").strip())
    return str(base / ".kernel_last_shutdown_at")


def new_session_id() -> str:
    return f"sess-{int(time.time())}-{uuid.uuid4().hex[:6]}"
