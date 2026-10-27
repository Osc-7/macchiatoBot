"""把运行时/LLM 错误交给已注册的上报通道。

设计要点：
- **只上报，不影响主流程**：所有异常在内部吞掉并记 warning，调用方可以
  ``asyncio.create_task(...)`` 直接 fire-and-forget。
- **同类错误限流**：同 (source, kind, provider) 在窗口期内只推一条，
  后续计数累积；避免上游挂掉时把 owner 刷爆。
- **发送不在这里**：本模块不依赖任何前端客户端。daemon 启动时由对应前端
  注册 sender，负责把卡片发到 owner 的会话。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from agent_core.llm.error_info import LLMErrorInfo

logger = logging.getLogger(__name__)

OperatorErrorSender = Callable[..., Awaitable[None]]
_operator_error_sender: Optional[OperatorErrorSender] = None

_DEDUPE: Dict[str, Tuple[float, int]] = {}
_DEDUPE_LOCK = threading.Lock()

DEFAULT_DEDUPE_SECONDS = 300.0
_SOURCE_LABELS = {
    "shuiyuan": "水源",
    "feishu": "飞书",
    "cli": "CLI",
    "subagent": "子任务",
}


def _should_send(key: str, window: float) -> Tuple[bool, int]:
    """返回 (是否发送, 本窗口内累计次数)。"""
    now = time.time()
    with _DEDUPE_LOCK:
        last_ts, count = _DEDUPE.get(key, (0.0, 0))
        if now - last_ts < window:
            _DEDUPE[key] = (last_ts, count + 1)
            return False, count + 1
        total = count + 1 if last_ts else 1
        _DEDUPE[key] = (now, 1)
        return True, total


def dedupe_key(source: str, error: LLMErrorInfo) -> str:
    """同类错误的限流键：同 (source, kind, provider) 合并，不按会话拆开。"""
    return f"{source}|{error.kind}|{error.provider or '-'}"


def reset_dedupe() -> None:
    """测试用：清空限流状态。"""
    with _DEDUPE_LOCK:
        _DEDUPE.clear()


def set_operator_error_sender(sender: Optional[OperatorErrorSender]) -> None:
    """注册或卸下错误上报通道。未注册时 notify_operator_error 不发送。"""
    global _operator_error_sender
    _operator_error_sender = sender


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\r", "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def build_error_card(
    *,
    error: LLMErrorInfo,
    source: str,
    session_id: str,
    user_id: Optional[str] = None,
    user_text: Optional[str] = None,
    stage: str = "模型请求",
    extra_lines: Optional[Dict[str, str]] = None,
    repeated: int = 1,
) -> Dict[str, Any]:
    """构造飞书 interactive card（schema 2.0）。"""
    source_label = _SOURCE_LABELS.get(source, source or "未知渠道")
    title = (
        f"{source_label}回复失败"
        if repeated <= 1
        else f"{source_label}回复失败（近期 {repeated} 次）"
    )

    lines = []
    lines.append(f"**渠道**　{source_label}")
    if session_id:
        lines.append(f"**会话**　`{session_id}`")
    if user_id:
        lines.append(f"**触发用户**　@{user_id}")
    if user_text:
        lines.append(f"**用户消息**　「{_truncate(user_text, 300)}」")
    lines.append(f"**失败阶段**　{stage}")
    lines.append("")
    lines.append(f"**错误类型**　{error.label}（`{error.kind}`）")
    status_bits = []
    if error.http_status:
        status_bits.append(f"HTTP {error.http_status}")
    if error.error_code:
        status_bits.append(str(error.error_code))
    if status_bits:
        lines.append(f"**状态码 / 错误码**　{' · '.join(status_bits)}")
    if error.provider or error.model:
        lines.append(
            f"**provider**　`{error.provider or '-'}` / `{error.model or '-'}`"
        )
    if error.exception_type:
        lines.append(f"**异常**　`{error.exception_type}`")
    lines.append(f"**时间**　{error.occurred_at}")
    if error.message:
        lines.append("")
        lines.append(f"**上游原文**　{_truncate(error.message, 400)}")
    if error.hint:
        lines.append(f"**说明**　{error.hint}")
    if repeated > 1:
        lines.append(f"**重复**　同窗口内累计 {repeated} 次，已对同类错误限流。")
    if extra_lines:
        for k, v in extra_lines.items():
            if v:
                lines.append(f"**{k}**　{_truncate(str(v), 300)}")

    markdown = "\n".join(lines)
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "summary": {"content": _truncate(title, 100)},
        },
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": _truncate(title, 40)},
            "subtitle": {"tag": "plain_text", "content": "运行时错误上报"},
            "padding": "12px 14px 12px 14px",
        },
        "body": {
            "direction": "vertical",
            "padding": "6px 14px 16px 14px",
            "vertical_spacing": "medium",
            "elements": [
                {
                    "tag": "markdown",
                    "content": markdown,
                    "text_align": "left",
                    "text_size": "normal_v2",
                    "margin": "0px 0px 0px 0px",
                }
            ],
        },
    }


def error_info_from_dict(data: Dict[str, Any]) -> LLMErrorInfo:
    """从 session metadata 里的 ``_error_info`` 还原对象（容错：字段缺失可接受）。"""
    if not isinstance(data, dict):
        return LLMErrorInfo()
    allowed = {
        "kind",
        "label",
        "message",
        "http_status",
        "error_code",
        "provider",
        "model",
        "exception_type",
        "retryable",
        "occurred_at",
        "hint",
        "request_id",
    }
    kwargs = {k: v for k, v in data.items() if k in allowed}
    try:
        return LLMErrorInfo(**kwargs)
    except Exception:  # noqa: BLE001
        return LLMErrorInfo(message=str(data))


def build_user_facing_error_card(error: LLMErrorInfo) -> Dict[str, Any]:
    """主会话失败时给用户看的错误说明：只写这次失败的原因。"""
    lines: list[str] = []
    if error.hint:
        lines.append(error.hint)
        lines.append("")
    lines.append(f"**错误**　{error.label}（`{error.kind}`）")
    bits = []
    if error.http_status:
        bits.append(f"HTTP {error.http_status}")
    if error.error_code:
        bits.append(str(error.error_code))
    if bits:
        lines.append(f"**状态码 / 错误码**　{' · '.join(bits)}")
    if error.provider or error.model:
        lines.append(
            f"**provider**　`{error.provider or '-'}` / `{error.model or '-'}`"
        )
    lines.append(f"**时间**　{error.occurred_at}")
    if error.message:
        lines.append("")
        lines.append(f"**上游原文**　{_truncate(error.message, 400)}")
    markdown = "\n".join(lines)
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "summary": {"content": _truncate(f"请求失败：{error.label}", 100)},
        },
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "请求失败"},
            "subtitle": {"tag": "plain_text", "content": error.label},
            "padding": "12px 14px 12px 14px",
        },
        "body": {
            "direction": "vertical",
            "padding": "6px 14px 16px 14px",
            "vertical_spacing": "medium",
            "elements": [
                {
                    "tag": "markdown",
                    "content": markdown,
                    "text_align": "left",
                    "text_size": "normal_v2",
                    "margin": "0px 0px 0px 0px",
                }
            ],
        },
    }


def _resolve_target(config: Any) -> Tuple[bool, str]:
    """解析通知目标：优先 error_reporting.notify_chat_id，否则用 automation 活动会话。"""
    enabled = True
    chat_id = ""
    er = getattr(config, "error_reporting", None)
    if er is not None:
        enabled = bool(getattr(er, "enabled", True))
        chat_id = (getattr(er, "notify_chat_id", None) or "").strip()
    if not chat_id:
        feishu_cfg = getattr(config, "feishu", None)
        if feishu_cfg is not None:
            if not getattr(feishu_cfg, "automation_activity_enabled", False):
                enabled = False
            chat_id = (
                getattr(feishu_cfg, "automation_activity_chat_id", "") or ""
            ).strip()
    return enabled and bool(chat_id), chat_id


async def notify_operator_error(
    *,
    error: LLMErrorInfo,
    source: str,
    session_id: str,
    user_id: Optional[str] = None,
    user_text: Optional[str] = None,
    stage: str = "模型请求",
    extra_lines: Optional[Dict[str, str]] = None,
    config: Any = None,
) -> bool:
    """把一条错误交给已注册的上报通道。任何失败都只记 warning，不抛。"""
    try:
        from agent_core.config import get_config

        cfg = config or get_config()
        enabled, chat_id = _resolve_target(cfg)
        if not enabled or not chat_id:
            logger.debug("error_notify: 未配置通知目标，跳过上报")
            return False
        sender = _operator_error_sender
        if sender is None:
            logger.info("error_notify: 没有注册上报通道，跳过上报")
            return False

        er = getattr(cfg, "error_reporting", None)
        window = float(
            getattr(er, "dedupe_seconds", DEFAULT_DEDUPE_SECONDS)
            or DEFAULT_DEDUPE_SECONDS
        )
        key = dedupe_key(source, error)
        should_send, repeated = _should_send(key, window)
        if not should_send:
            logger.info(
                "error_notify: 同类错误限流中（%s, 累计 %d 次），跳过推送",
                key,
                repeated,
            )
            return False

        timeout = 30.0
        feishu_cfg = getattr(cfg, "feishu", None)
        if feishu_cfg is not None:
            try:
                timeout = float(getattr(feishu_cfg, "timeout_seconds", 30.0) or 30.0)
            except (TypeError, ValueError):
                timeout = 30.0
        await sender(
            chat_id=chat_id,
            error=error,
            source=source,
            session_id=session_id,
            user_id=user_id,
            user_text=user_text,
            stage=stage,
            extra_lines=extra_lines,
            repeated=repeated,
            timeout_seconds=max(timeout, 30.0),
        )
        logger.info(
            "error_notify: 已上报 %s 到 %s（kind=%s）", source, chat_id, error.kind
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("error_notify: 上报失败: %s", exc)
        return False
