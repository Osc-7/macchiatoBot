"""将 ask_user 挂起事件推送到飞书会话（需 payload.feishu_chat_id）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from agent_core.config import get_config
from agent_core.permissions.ask_user_registry import set_ask_user_notify_hook

from .ask_user_card import build_ask_user_card_from_registry_snapshot
from .client import FeishuClient

logger = logging.getLogger(__name__)


def _format_fallback_text(batch_id: str, payload: Dict[str, Any]) -> str:
    qs = payload.get("questions") or []
    lines = ["[Agent 提问 · ask_user]", f"batch_id：`{batch_id}`"]
    if isinstance(qs, list):
        for i, q in enumerate(qs):
            if not isinstance(q, dict):
                continue
            pid = str(q.get("id") or f"q{i + 1}")
            pr = str(q.get("prompt") or "").strip()
            opts = q.get("options") or []
            ol = ", ".join(str(x) for x in opts) if isinstance(opts, list) else ""
            lines.append(f"- {pid}: {pr}")
            if ol:
                lines.append(f"  选项：{ol}")
    lines.append(
        "（说明：本应发送可点选的交互卡片；若你只看到本段纯文本，表示卡片接口报错已降级，"
        "常见原因是回调数据过长。请重启 automation_daemon 后重试，并查看日志中的「ask_user notify: card send failed」。）"
    )
    return "\n".join(lines)


async def send_ask_user_feishu_cards(batch_id: str, payload: Dict[str, Any]) -> None:
    """发送 ask_user 提问卡；飞书网关经 IPC 顺序调用时可保证出现在 tool trace 之后。"""
    chat_id = str(payload.get("feishu_chat_id") or "").strip()
    if not chat_id:
        logger.debug(
            "ask_user notify skipped (no feishu_chat_id), session_id=%s",
            payload.get("session_id"),
        )
        return

    questions = payload.get("questions") or []
    if not isinstance(questions, list) or not questions:
        logger.warning("ask_user notify: empty questions batch_id=%s", batch_id)
        return

    custom_label = str(payload.get("custom_option_label") or "").strip() or "其他（请填写具体说明）"

    try:
        cfg = get_config()
        to = float(getattr(cfg.feishu, "timeout_seconds", 30.0) or 30.0)
        client = FeishuClient(timeout_seconds=to)
        qlist = [q for q in questions if isinstance(q, dict)]
        init_snap: Dict[str, Any] = {
            "batch_id": batch_id,
            "questions": qlist,
            "partial": {},
            "custom_option_label": custom_label,
            "done": False,
        }
        fallback = _format_fallback_text(batch_id, payload)
        any_card_ok = False
        for qi, q in enumerate(qlist):
            qid = str(q.get("id") or "").strip() or f"q{qi + 1}"
            card = build_ask_user_card_from_registry_snapshot(init_snap, qid)
            try:
                await client.send_interactive_card(chat_id=chat_id, card=card)
                any_card_ok = True
            except Exception as card_exc:  # noqa: BLE001
                logger.warning(
                    "ask_user notify: card send failed qid=%s: %s",
                    qid,
                    card_exc,
                    exc_info=True,
                )
        if not any_card_ok:
            await client.send_text_message(chat_id=chat_id, text=fallback)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ask_user notify: feishu send failed chat_id=%s: %s", chat_id, exc)


def _on_ask_user_pending(batch_id: str, payload: Dict[str, Any]) -> None:
    """无 IPC 顺序转发时由 daemon 直连飞书（如测试或旧路径）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("ask_user notify: no running event loop")
        return

    loop.create_task(send_ask_user_feishu_cards(batch_id, payload))


def install_feishu_ask_user_notify_hook() -> None:
    """在 automation_daemon 进程启动时调用，注册全局 ask_user 通知。"""
    set_ask_user_notify_hook(_on_ask_user_pending)
