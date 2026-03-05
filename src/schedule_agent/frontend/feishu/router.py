from __future__ import annotations

"""
飞书回调路由。

提供 /feishu/event 入口：
- 处理 URL 验证（url_verification）
- 接收 im.message.receive_v1 事件，将文本消息转发给 automation daemon，并通过飞书回复结果

注意：飞书在网络抖动或服务重启时可能会重试/重放事件，这里实现了简单的
event_id / message_id 级别去重，避免同一条消息被多次处理。
"""

import logging
import time
from collections import deque
from typing import Any, Deque, Dict, Set, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from schedule_agent.config import get_config

from .client import FeishuClient
from .config import get_feishu_config
from .event_models import FeishuChallengeRequest, FeishuEventEnvelope
from .ipc_bridge import AutomationDaemonUnavailable, FeishuIPCBridge
from .session_mapping import map_event_to_session

logger = logging.getLogger(__name__)

router = APIRouter()

# 简单的内存去重缓存：只保留最近若干条 event_id，带 TTL
_DEDUP_CACHE_SIZE = 1024
_DEDUP_TTL_SECONDS = 300  # 5 分钟
_dedup_events: Deque[Tuple[str, float]] = deque()
_dedup_index: Set[str] = set()


def _is_duplicate_event(event_key: str) -> bool:
    """基于 event_key（优先 event_id，其次 message_id）做幂等去重。"""
    if not event_key:
        return False
    now = time.time()

    # 清理过期条目
    while _dedup_events:
        key, ts = _dedup_events[0]
        if now - ts > _DEDUP_TTL_SECONDS:
            _dedup_events.popleft()
            _dedup_index.discard(key)
        else:
            break

    if event_key in _dedup_index:
        return True

    _dedup_events.append((event_key, now))
    _dedup_index.add(event_key)
    if len(_dedup_events) > _DEDUP_CACHE_SIZE:
        old_key, _ = _dedup_events.popleft()
        _dedup_index.discard(old_key)
    return False


def _build_ipc_bridge() -> FeishuIPCBridge:
    cfg = get_config()
    return FeishuIPCBridge(timeout_seconds=cfg.llm.request_timeout_seconds)


def _build_feishu_client() -> FeishuClient:
    cfg = get_feishu_config()
    return FeishuClient(timeout_seconds=cfg.timeout_seconds)


@router.post("/feishu/event")
async def handle_feishu_event(request: Request) -> JSONResponse:
    """
    飞书事件回调处理入口。

    - type == url_verification: 返回 challenge 完成 URL 验证
    - 其他：按 header.event_type 分发，目前仅处理 im.message.receive_v1
    """
    cfg = get_feishu_config()
    if not cfg.enabled:
        raise HTTPException(status_code=503, detail="Feishu integration is disabled in config.yaml")

    body: Dict[str, Any] = await request.json()
    event_type = str(body.get("type") or "")

    # URL 验证
    if event_type == "url_verification":
        challenge_req = FeishuChallengeRequest(**body)
        if cfg.verification_token and challenge_req.token and challenge_req.token != cfg.verification_token:
            raise HTTPException(status_code=403, detail="invalid verification token")
        return JSONResponse({"challenge": challenge_req.challenge})

    # 普通事件（schema 2.0）
    envelope = FeishuEventEnvelope(**body)
    header = envelope.header
    if cfg.verification_token and header.token and header.token != cfg.verification_token:
        raise HTTPException(status_code=403, detail="invalid verification token")

    # 去重：优先使用 event_id，退化到 message_id
    msg_event = envelope.event
    event_key = header.event_id or getattr(msg_event.message, "message_id", "")
    if _is_duplicate_event(event_key):
        logger.info("ignore duplicate feishu event: %s", event_key)
        return JSONResponse({"code": 0, "msg": "duplicate_ignored"})

    if header.event_type != "im.message.receive_v1":
        # 非消息事件，直接忽略
        logger.debug("ignore feishu event_type=%s", header.event_type)
        return JSONResponse({"code": 0, "msg": "ignored"})

    event = envelope.event
    msg = event.message
    if msg.message_type != "text":
        logger.debug("ignore non-text message_type=%s", msg.message_type)
        return JSONResponse({"code": 0, "msg": "ignored_non_text"})

    text = msg.text.strip()
    if not text:
        return JSONResponse({"code": 0, "msg": "ignored_empty"})

    session_id, meta = map_event_to_session(event)
    metadata: Dict[str, Any] = {
        **meta,
        "source": "feishu",
        "feishu_event_id": header.event_id,
    }

    ipc = _build_ipc_bridge()
    try:
        result = await ipc.send_message(
            session_id=session_id,
            text=text,
            metadata=metadata,
            owner_id="root",
            source="feishu",
        )
    except AutomationDaemonUnavailable as exc:
        logger.warning("automation daemon unavailable for feishu message: %s", exc)
        # 告知飞书侧“服务暂时不可用”，避免重复重试
        return JSONResponse(
            {"code": 1, "msg": "automation daemon unavailable"},
            status_code=503,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to process feishu message via automation daemon: %s", exc)
        return JSONResponse(
            {"code": 1, "msg": "internal error"},
            status_code=500,
        )

    # 将 Agent 回复通过飞书再发回用户
    feishu_client = _build_feishu_client()
    try:
        await feishu_client.send_text_message(chat_id=msg.chat_id, text=result.output_text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to send feishu reply message: %s", exc)

    # 按飞书约定返回 200 + code/msg
    return JSONResponse({"code": 0, "msg": "ok"})

