"""根据配置将 Agent 最终回复发到飞书（纯文本或 Markdown 卡片）。"""

from __future__ import annotations

import logging

from agent_core.config import get_config

from .client import FeishuClient
from .interactive_cards import AssistantReplyPhase, build_agent_reply_markdown_card

logger = logging.getLogger(__name__)


def _normalized_reply_format() -> str:
    fmt = (get_config().feishu.reply_format or "markdown_card").strip().lower()
    if fmt not in ("plain", "markdown_card"):
        fmt = "markdown_card"
    return fmt


async def send_feishu_agent_reply(
    *,
    client: FeishuClient,
    chat_id: str,
    output_text: str,
    markdown_card_header_title: str = "回复",
    reply_phase: AssistantReplyPhase = "final",
) -> None:
    """
    按 feishu.reply_format 发送助手文本。

    - markdown_card：交互卡片内 Markdown（不走 filter_markdown_for_feishu）
    - plain：纯文本消息（仍走 send_text_message 内的 Markdown→可读纯文本过滤）
    """
    text = (output_text or "").strip()
    if not text:
        return
    fmt = _normalized_reply_format()
    try:
        if fmt == "markdown_card":
            card = build_agent_reply_markdown_card(
                text,
                header_title=markdown_card_header_title,
                reply_phase=reply_phase,
            )
            await client.send_interactive_card(chat_id=chat_id, card=card)
        else:
            await client.send_text_message(chat_id=chat_id, text=text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("send_feishu_agent_reply failed: %s", exc)
        raise


async def send_feishu_agent_final_reply(
    *,
    client: FeishuClient,
    chat_id: str,
    output_text: str,
) -> None:
    """发送一轮对话的最终助手输出；空文本不发送。"""
    await send_feishu_agent_reply(
        client=client,
        chat_id=chat_id,
        output_text=output_text,
        markdown_card_header_title="回复",
        reply_phase="final",
    )
