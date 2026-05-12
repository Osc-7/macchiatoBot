"""飞书端「下一轮再带附件」队列：纯图片/文件消息先入队，用户下一条文字再触发 Agent。"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Tuple

from agent_core.content import ContentReference

logger = logging.getLogger(__name__)

_MAX_QUEUED_REFS = 32
_TTL_SECONDS = 3600.0


def feishu_slash_clears_attachment_queue(text: str) -> bool:
    """与 /clear 一致时清空排队附件。"""
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return False
    parts = raw[1:].strip().split()
    return bool(parts) and parts[0].lower() == "clear"


_lock = threading.Lock()
_state: Dict[str, Tuple[List[ContentReference], float]] = {}


def _prune_locked(now: float) -> None:
    dead = [k for k, (_refs, exp) in _state.items() if exp <= now]
    for k in dead:
        del _state[k]


def queue_attachments_for_next_turn(session_id: str, refs: List[ContentReference]) -> None:
    """将附件追加到会话队列，等待下一条用户消息与 Agent 一并处理。"""
    sid = (session_id or "").strip()
    if not sid or not refs:
        return
    now = time.time()
    expiry = now + _TTL_SECONDS
    with _lock:
        _prune_locked(now)
        existing, _old_exp = _state.get(sid, ([], expiry))
        merged = list(existing) + list(refs)
        if len(merged) > _MAX_QUEUED_REFS:
            drop = len(merged) - _MAX_QUEUED_REFS
            logger.warning(
                "feishu pending attachments truncated for session=%s drop=%s",
                sid,
                drop,
            )
            merged = merged[-_MAX_QUEUED_REFS :]
        _state[sid] = (merged, expiry)


def take_queued_attachments(session_id: str) -> List[ContentReference]:
    """取出并清空该会话已排队的附件（在即将发起 run_turn 前调用）。"""
    sid = (session_id or "").strip()
    if not sid:
        return []
    now = time.time()
    with _lock:
        _prune_locked(now)
        entry = _state.pop(sid, None)
        if not entry:
            return []
        refs, _exp = entry
        return list(refs)


def clear_queued_attachments(session_id: str) -> None:
    """丢弃排队附件（如 /clear）。"""
    sid = (session_id or "").strip()
    if not sid:
        return
    now = time.time()
    with _lock:
        _prune_locked(now)
        _state.pop(sid, None)
