"""飞书侧的 owner 错误上报。由 daemon 启动时注册，kernel 不直接发飞书。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agent_core.llm.error_info import LLMErrorInfo

from .client import FeishuClient

logger = logging.getLogger(__name__)


async def send_operator_error_card(
    *,
    chat_id: str,
    error: LLMErrorInfo,
    source: str,
    session_id: str,
    user_id: Optional[str] = None,
    user_text: Optional[str] = None,
    stage: str = "模型请求",
    extra_lines: Optional[Dict[str, str]] = None,
    repeated: int = 1,
    timeout_seconds: float = 30.0,
) -> None:
    """把 owner 错误说明发成飞书卡片。"""
    from system.kernel.error_notify import build_error_card

    card: Dict[str, Any] = build_error_card(
        error=error,
        source=source,
        session_id=session_id,
        user_id=user_id,
        user_text=user_text,
        stage=stage,
        extra_lines=extra_lines,
        repeated=repeated,
    )
    client = FeishuClient(timeout_seconds=max(float(timeout_seconds), 30.0))
    await client.send_interactive_card(chat_id=chat_id, card=card)
    logger.info(
        "operator notify: sent %s kind=%s chat_id=%s", source, error.kind, chat_id
    )


def install_feishu_operator_error_sender() -> None:
    """在 automation daemon 进程启动时调用。"""
    from system.kernel.error_notify import set_operator_error_sender

    set_operator_error_sender(send_operator_error_card)
