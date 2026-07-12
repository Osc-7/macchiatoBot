"""
成体系记忆文档路径解析（memory_update 白名单）
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from agent_core.agent.memory_paths import (
    effective_memory_namespace_from_execution_context,
    resolve_memory_owner_paths,
)
from agent_core.config import Config

VALID_MEMORY_DOCS = frozenset({"memory", "soul", "identity", "user", "agents"})

_SYSTEM_PROMPTS_DIR = (
    Path(__file__).resolve().parent.parent / "prompts" / "system"
)


def normalize_memory_doc_name(doc: str) -> Tuple[Optional[str], Optional[str]]:
    """
    解析 doc 参数为白名单名。

    支持 ``memory``、``soul``、``memory/soul``（取最后一段）等。
    """
    raw = (doc or "").strip().lower().replace("\\", "/")
    if not raw:
        return None, "缺少 doc 参数"
    name = raw.split("/")[-1] if "/" in raw else raw
    if name not in VALID_MEMORY_DOCS:
        allowed = ", ".join(sorted(VALID_MEMORY_DOCS))
        return None, f"非法 doc: {doc!r}。允许: {allowed}"
    return name, None


def resolve_memory_doc_path(
    doc: str,
    *,
    config: Config,
    exec_ctx: Optional[dict] = None,
) -> Tuple[Optional[Path], Optional[str]]:
    """将白名单 doc 名解析为 daemon 本地绝对路径。"""
    name, err = normalize_memory_doc_name(doc)
    if err or name is None:
        return None, err

    ctx = exec_ctx or {}
    if name == "memory":
        mem_cfg = getattr(config, "memory", None)
        if not mem_cfg:
            return None, "记忆系统未配置"
        fe, uid = effective_memory_namespace_from_execution_context(ctx)
        paths = resolve_memory_owner_paths(mem_cfg, uid, config=config, source=fe)
        return Path(paths["memory_md_path"]).resolve(), None

    filename = f"{name}.md"
    path = (_SYSTEM_PROMPTS_DIR / filename).resolve()
    if not path.exists() and name == "user":
        alt = (_SYSTEM_PROMPTS_DIR / "user.example.md").resolve()
        if alt.exists():
            return alt, None
    return path, None
