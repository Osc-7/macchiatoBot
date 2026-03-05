from __future__ import annotations

"""
飞书会话映射工具。

负责将飞书的 user/chat 信息映射为内部的 Schedule Agent session_id。
"""

from typing import Dict, Tuple

from .event_models import FeishuMessageEvent


def map_event_to_session(event: FeishuMessageEvent) -> Tuple[str, Dict[str, str]]:
    """
    根据飞书消息事件生成内部 session_id 以及元数据。

    返回:
        session_id: 形如 feishu:user:{open_id} 或 feishu:chat:{chat_id}
        metadata:   可附加到 AgentRunInput.metadata 的标识信息
    """
    sender_ids = event.sender.sender_id
    msg = event.message

    open_id = (sender_ids.open_id or "").strip()
    user_id = (sender_ids.user_id or "").strip()
    chat_id = (msg.chat_id or "").strip()
    chat_type = (msg.chat_type or "").strip() or "p2p"

    if chat_type == "p2p":
        key = open_id or user_id or chat_id or "unknown"
        session_id = f"feishu:user:{key}"
    else:
        key = chat_id or open_id or user_id or "unknown"
        session_id = f"feishu:chat:{key}"

    metadata: Dict[str, str] = {
        "feishu_open_id": open_id,
        "feishu_user_id": user_id,
        "feishu_chat_id": chat_id,
        "feishu_chat_type": chat_type,
        "feishu_session_id": session_id,
    }
    return session_id, metadata

