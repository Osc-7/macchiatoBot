"""将 request_permission 挂起事件推送到飞书会话（需 __execution_context__.feishu_chat_id）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from agent_core.config import get_config
from agent_core.permissions.wait_registry import set_permission_notify_hook

from .client import FeishuClient
from .permission_card import build_permission_request_card

logger = logging.getLogger(__name__)


def _format_permission_message(permission_id: str, payload: Dict[str, Any]) -> str:
    summary = str(payload.get("summary") or "").strip()
    kind = str(payload.get("kind") or "").strip()
    timeout = payload.get("timeout_seconds")
    lines = [
        "[权限申请]",
        f"摘要：{summary}",
    ]
    if kind:
        lines.append(f"类别：{kind}")
    if timeout is not None:
        lines.append(f"等待超时（秒）：{timeout}")
    lines.append(f"permission_id：{permission_id}")
    lines.append("超时未批准将视为拒绝。")
    return "\n".join(lines)


async def send_permission_feishu_card(
    permission_id: str, payload: Dict[str, Any]
) -> None:
    """发送权限申请卡；飞书网关经 IPC 顺序调用时可保证出现在 tool trace 之后。"""
    chat_id = str(payload.get("feishu_chat_id") or "").strip()
    if not chat_id:
        logger.debug(
            "permission notify skipped (no feishu_chat_id), session_id=%s",
            payload.get("session_id"),
        )
        return
    text = _format_permission_message(permission_id, payload)
    try:
        cfg = get_config()
        to = float(getattr(cfg.feishu, "timeout_seconds", 30.0) or 30.0)
        client = FeishuClient(timeout_seconds=to)
        pfx = str(payload.get("path_prefix") or "").strip() or None
        card = build_permission_request_card(
            permission_id=permission_id,
            summary=str(payload.get("summary") or ""),
            kind=str(payload.get("kind") or ""),
            timeout_seconds=payload.get("timeout_seconds"),
            path_prefix=pfx,
        )
        try:
            await client.send_interactive_card(chat_id=chat_id, card=card)
        except Exception as card_exc:  # noqa: BLE001
            logger.warning(
                "permission notify: card send failed, fallback to text: %s",
                card_exc,
            )
            await client.send_text_message(chat_id=chat_id, text=text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "permission notify: feishu send failed chat_id=%s: %s", chat_id, exc
        )


def _on_permission_pending(permission_id: str, payload: Dict[str, Any]) -> None:
    """无 IPC 顺序转发时由 daemon 直连飞书（如测试或旧路径）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("permission notify: no running event loop")
        return

    loop.create_task(send_permission_feishu_card(permission_id, payload))


def install_feishu_permission_notify_hook() -> None:
    """在 automation_daemon 进程启动时调用，注册全局 permission 通知。"""
    set_permission_notify_hook(_on_permission_pending)
