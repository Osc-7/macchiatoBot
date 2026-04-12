"""
飞书回调路由。

提供 /feishu/event 入口：
- 处理 URL 验证（url_verification）
- 接收 im.message.receive_v1 事件，将文本消息转发给 automation daemon，并通过飞书回复结果

注意：飞书在网络抖动或服务重启时可能会重试/重放事件，这里实现了简单的
event_id / message_id 级别去重，避免同一条消息被多次处理。
"""

from __future__ import annotations
import json
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, Set, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from agent_core.config import get_config

from .client import FeishuClient
from .config import get_feishu_config
from .content_parser import parse_feishu_message
from .event_models import FeishuChallengeRequest, FeishuEventEnvelope
from .ipc_bridge import (
    AutomationDaemonUnavailable,
    FeishuIPCBridge,
    MSG_FEISHU_DAEMON_UNAVAILABLE,
    format_feishu_processing_error,
    get_feishu_push_forwarder,
    register_feishu_push_session,
    send_feishu_error_notice,
    try_handle_slash_command_via_ipc,
)
from .session_mapping import map_event_to_session
from .card_callback import handle_feishu_card_action
from .event_crypto import decrypt_feishu_event_body, verify_feishu_http_signature
from .reply_dispatch import send_feishu_agent_final_reply


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
    return FeishuIPCBridge(
        ipc_timeout_seconds=cfg.feishu.automation_ipc_timeout_seconds,
    )


def _build_feishu_client() -> FeishuClient:
    cfg = get_feishu_config()
    return FeishuClient(timeout_seconds=cfg.timeout_seconds)


@router.post("/feishu/event")
async def handle_feishu_event(request: Request) -> JSONResponse:
    """
    飞书事件回调处理入口。

    - type == url_verification: 返回 challenge 完成 URL 验证
    - header.event_type == card.action.trigger: 卡片按钮回传（权限批准/拒绝）
    - 其他：按 header.event_type 分发，处理 im.message.receive_v1
    """
    cfg = get_feishu_config()
    if not cfg.enabled:
        raise HTTPException(
            status_code=503, detail="Feishu integration is disabled in config.yaml"
        )

    body_bytes = await request.body()
    try:
        raw: Dict[str, Any] = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("feishu event: invalid json body: %s", exc)
        raise HTTPException(status_code=400, detail="invalid json") from exc

    if not verify_feishu_http_signature(
        body_bytes=body_bytes,
        encrypt_key=cfg.encrypt_key,
        headers=request.headers,
    ):
        # 非 HTTP 200 时飞书客户端报 200671；签名校验失败仍返回 200 + toast
        return JSONResponse(
            status_code=200,
            content={
                "toast": {
                    "type": "error",
                    "content": "签名校验失败，请核对 feishu.encrypt_key 与开放平台是否一致",
                }
            },
        )

    try:
        body = decrypt_feishu_event_body(raw, cfg.encrypt_key)
    except ValueError as exc:
        logger.warning("feishu event decrypt: %s", exc)
        return JSONResponse(
            status_code=200,
            content={"toast": {"type": "error", "content": str(exc)}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("feishu event decrypt failed: %s", exc)
        return JSONResponse(
            status_code=200,
            content={"toast": {"type": "error", "content": "事件解密失败"}},
        )

    event_type = str(body.get("type") or "")

    # URL 验证
    if event_type == "url_verification":
        challenge_req = FeishuChallengeRequest(**body)
        if (
            cfg.verification_token
            and challenge_req.token
            and challenge_req.token != cfg.verification_token
        ):
            raise HTTPException(status_code=403, detail="invalid verification token")
        return JSONResponse({"challenge": challenge_req.challenge})

    # 卡片按钮回传（需在开平订阅 card.action.trigger，与 HTTP 回调同 URL）
    header_raw = body.get("header") or {}
    if str(header_raw.get("event_type") or "") == "card.action.trigger":
        event_key_cb = str(header_raw.get("event_id") or "")
        if event_key_cb and _is_duplicate_event(event_key_cb):
            return JSONResponse(
                content={
                    "toast": {
                        "type": "info",
                        "content": "重复事件已忽略",
                    }
                }
            )
        return await handle_feishu_card_action(body)

    # 普通事件（schema 2.0）
    envelope = FeishuEventEnvelope(**body)
    header = envelope.header
    if (
        cfg.verification_token
        and header.token
        and header.token != cfg.verification_token
    ):
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
    # 支持 text / image / file / media / audio / post（富文本内嵌图片）
    supported_types = ("text", "image", "file", "media", "audio", "post")
    if msg.message_type not in supported_types:
        logger.debug("ignore unsupported message_type=%s", msg.message_type)
        return JSONResponse({"code": 0, "msg": "ignored_unsupported"})

    content_refs, text = parse_feishu_message(
        message_id=msg.message_id,
        message_type=msg.message_type,
        content=msg.content,
    )
    # 纯媒体消息时 text 为占位描述；若两者都空则忽略
    if not text and not content_refs:
        return JSONResponse({"code": 0, "msg": "ignored_empty"})

    # 便于配置 automation_activity_chat_id：在日志中输出当前会话 chat_id
    logger.info(
        "feishu message received chat_id=%s (可填入 config.feishu.automation_activity_chat_id 以在此会话接收自动化通知)",
        msg.chat_id,
    )

    session_id, meta = map_event_to_session(event)
    metadata: Dict[str, Any] = {**meta, "feishu_event_id": header.event_id}
    if content_refs:
        metadata["content_refs"] = [r.to_dict() for r in content_refs]

    # 斜杠指令：仅对纯文本消息且以 / 开头时处理
    if not content_refs and text.strip().startswith("/"):
        try:
            reply = await try_handle_slash_command_via_ipc(
                session_id=session_id,
                text=text,
                timeout_seconds=get_config().feishu.automation_ipc_timeout_seconds,
            )
            if reply is not None:
                feishu_client = _build_feishu_client()
                try:
                    await feishu_client.send_text_message(
                        chat_id=msg.chat_id, text=reply
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "failed to send feishu slash command reply: %s", exc
                    )
                return JSONResponse({"code": 0, "msg": "ok"})
        except AutomationDaemonUnavailable:
            # fallthrough，后续 send_message 会返回 503
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("slash command failed, fallback to agent: %s", exc)

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
        await send_feishu_error_notice(
            chat_id=msg.chat_id,
            text=MSG_FEISHU_DAEMON_UNAVAILABLE,
            timeout_seconds=cfg.timeout_seconds,
        )
        # 告知飞书侧“服务暂时不可用”，避免重复重试
        return JSONResponse(
            {"code": 1, "msg": "automation daemon unavailable"},
            status_code=503,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "failed to process feishu message via automation daemon: %s", exc
        )
        await send_feishu_error_notice(
            chat_id=msg.chat_id,
            text=format_feishu_processing_error(exc),
            timeout_seconds=cfg.timeout_seconds,
        )
        return JSONResponse(
            {"code": 1, "msg": "internal error"},
            status_code=500,
        )

    # 将 Agent 回复通过飞书再发回用户（流式 PATCH 已完成时跳过重复卡片）
    feishu_client = _build_feishu_client()
    try:
        if not (result.metadata or {}).get("feishu_skip_final_reply"):
            await send_feishu_agent_final_reply(
                client=feishu_client,
                chat_id=msg.chat_id,
                output_text=result.output_text,
            )
        attachments = getattr(result, "attachments", None)
        if attachments:
            await feishu_client.send_reply_attachments(
                chat_id=msg.chat_id, attachments=attachments
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to send feishu reply message: %s", exc)

    # 注册 push 转发：subagent 完成等 inject_turn 结果将经 [out] 队列推送到本会话
    if msg.chat_id:
        get_feishu_push_forwarder().start()
        register_feishu_push_session(
            session_id=session_id,
            chat_id=msg.chat_id,
            ttl_seconds=300.0,
        )

    # 按飞书约定返回 200 + code/msg
    return JSONResponse({"code": 0, "msg": "ok"})
