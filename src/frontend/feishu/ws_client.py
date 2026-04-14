"""
基于飞书官方 Python SDK (lark-oapi) 的长连接事件网关。

使用方式:
    1. 在 config.yaml 中配置 feishu.app_id / feishu.app_secret，并将 feishu.enabled 设为 true
    2. 启动 automation_daemon.py，确保 Automation IPC 可用
    3. 运行:

        source init.sh
        python feishu_ws_gateway.py

    4. 在飞书开放平台为应用开启「长连接接收事件」模式，使用 app_id/app_secret 鉴权

本模块复用现有 HTTP 回调实现中的会话映射、去重与 IPC 逻辑，只是将事件来源
从 Webhook Request URL 换成飞书的 WebSocket 长连接，无需公网 IP / ngrok。
卡片「批准/拒绝」的 card.action.trigger 也由长连接投递，须注册
``register_p2_card_action_trigger``，否则会报 processor not found（客户端 200671）。
"""

from __future__ import annotations
import asyncio
import logging
import threading
from typing import Any

import lark_oapi as lark

from agent_core.config import get_config

from .client import FeishuClient
from .content_parser import parse_feishu_message
from .event_models import (
    FeishuMessage,
    FeishuMessageEvent,
    FeishuSender,
    FeishuSenderId,
)
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
from .router import _is_duplicate_event  # 复用去重缓存
from .reply_dispatch import send_feishu_agent_final_reply
from .session_mapping import map_event_to_session

logger = logging.getLogger(__name__)


def _handle_p2_card_action_trigger(data: Any) -> Any:
    """
    长连接上的 card.action.trigger（与 HTTP 回调语义一致）。

    若未注册此处理器，SDK 会报 processor not found，客户端表现为 200671。
    批准/拒绝须通过 IPC 转发到 automation_daemon（Future 仅存在于 daemon 进程）。
    """
    import asyncio

    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackToast,
        P2CardActionTriggerResponse,
    )

    from .card_callback import resolve_card_via_daemon_ipc

    cfg = get_config().feishu
    hdr = getattr(data, "header", None)
    if (
        hdr
        and cfg.verification_token
        and getattr(hdr, "token", None)
        and hdr.token != cfg.verification_token
    ):
        t = CallBackToast()
        t.type = "error"
        t.content = "verification_token 与开放平台配置不一致"
        r = P2CardActionTriggerResponse()
        r.toast = t
        return r

    event_key = str(getattr(hdr, "event_id", None) or "") if hdr else ""
    if event_key and _is_duplicate_event(event_key):
        t = CallBackToast()
        t.type = "info"
        t.content = "重复事件已忽略"
        r = P2CardActionTriggerResponse()
        r.toast = t
        return r

    ev = getattr(data, "event", None)
    action = getattr(ev, "action", None) if ev else None
    raw_val = getattr(action, "value", None) if action else None
    form_value = getattr(action, "form_value", None) if action else None
    if form_value is not None and not isinstance(form_value, dict):
        form_value = None

    try:
        kind, msg, card_dict = asyncio.run(
            resolve_card_via_daemon_ipc(raw_val, form_value=form_value)
        )
    except RuntimeError:
        # 极少数环境下当前线程已有运行中的 loop
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            kind, msg, card_dict = pool.submit(
                lambda: asyncio.run(
                    resolve_card_via_daemon_ipc(raw_val, form_value=form_value)
                )
            ).result(timeout=35.0)

    t = CallBackToast()
    t.type = kind
    t.content = msg
    r = P2CardActionTriggerResponse()
    r.toast = t
    if card_dict is not None:
        from lark_oapi.event.callback.model.p2_card_action_trigger import CallBackCard

        c = CallBackCard()
        c.type = "raw"
        c.data = card_dict
        r.card = c
    return r


async def _handle_im_message_event_async(data: Any) -> None:
    """
    处理来自 lark-oapi 的 P2ImMessageReceiveV1 事件。

    这里不直接依赖具体类型，只按飞书文档约定访问字段，避免 SDK 版本差异带来的类型问题。
    """
    cfg = get_config()
    feishu_cfg = cfg.feishu
    if not feishu_cfg.enabled:
        logger.warning("Feishu integration disabled in config, ignore ws event")
        return

    # data.event.message / data.event.sender 结构与 HTTP 事件 schema 2.0 对齐
    try:
        event_obj = data.event  # type: ignore[attr-defined]
        msg = event_obj.message  # type: ignore[attr-defined]
        sender = event_obj.sender  # type: ignore[attr-defined]
    except AttributeError:
        logger.warning("Received unexpected Feishu ws event payload, skip: %r", data)
        return

    # 支持 text / image / file / media / audio / post（富文本内嵌图片）
    message_type = getattr(msg, "message_type", None) or ""
    supported_types = ("text", "image", "file", "media", "audio", "post")
    if message_type not in supported_types:
        logger.debug("ignore unsupported ws message_type=%s", message_type)
        return

    # 去重：基于 message_id 做幂等
    message_id = getattr(msg, "message_id", "") or ""
    if _is_duplicate_event(message_id):
        logger.info("ignore duplicate feishu ws message: %s", message_id)
        return

    # 构造我们自己的 FeishuMessageEvent 模型，复用现有会话映射逻辑
    sender_id_obj = getattr(sender, "sender_id", None)
    feishu_sender = FeishuSender(
        sender_id=FeishuSenderId(
            open_id=getattr(sender_id_obj, "open_id", None),
            user_id=getattr(sender_id_obj, "user_id", None),
            union_id=getattr(sender_id_obj, "union_id", None),
        ),
        sender_type=getattr(sender, "sender_type", "user"),
        tenant_key=getattr(sender, "tenant_key", None),
    )
    feishu_message = FeishuMessage(
        message_id=message_id,
        chat_id=getattr(msg, "chat_id", "") or "",
        chat_type=getattr(msg, "chat_type", "") or "p2p",
        message_type=message_type,
        content=getattr(msg, "content", "") or "",
    )
    event_model = FeishuMessageEvent(sender=feishu_sender, message=feishu_message)

    raw_content = getattr(msg, "content", "") or ""
    content_refs, text = parse_feishu_message(
        message_id=message_id,
        message_type=message_type,
        content=raw_content,
    )
    # 图片/富文本消息若未解析出 content_refs，记录便于排查
    if message_type in ("image", "post") and not content_refs and raw_content:
        logger.debug(
            "feishu %s message parsed but no content_refs: content_preview=%s",
            message_type,
            (raw_content[:200] + "..." if len(raw_content) > 200 else raw_content),
        )
    if not text and not content_refs:
        logger.debug("ignore empty ws message")
        return

    # 便于配置 automation_activity_chat_id：在日志中输出当前会话 chat_id
    logger.info(
        "feishu message received chat_id=%s (可填入 config.feishu.automation_activity_chat_id 以在此会话接收自动化通知)",
        feishu_message.chat_id,
    )

    session_id, meta = map_event_to_session(event_model)
    metadata = {
        **meta,
        "feishu_message_id": message_id,
        "feishu_chat_id": feishu_message.chat_id,
        "feishu_chat_type": feishu_message.chat_type,
    }
    if content_refs:
        metadata["content_refs"] = [r.to_dict() for r in content_refs]

    # 斜杠指令：仅对纯文本消息且以 / 开头时处理
    if not content_refs and text.strip().startswith("/"):
        try:
            reply = await try_handle_slash_command_via_ipc(
                session_id=session_id,
                text=text,
                socket_path=None,
                timeout_seconds=feishu_cfg.automation_ipc_timeout_seconds,
            )
            if reply is not None:
                feishu_client = FeishuClient(timeout_seconds=feishu_cfg.timeout_seconds)
                try:
                    await feishu_client.send_text_message(
                        chat_id=feishu_message.chat_id,
                        text=reply,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "failed to send feishu slash command reply: %s", exc
                    )
                return
        except AutomationDaemonUnavailable as exc:
            logger.warning(
                "automation daemon unavailable for feishu slash command: %s", exc
            )
            #  fallthrough to send_message，会再次触发 AutomationDaemonUnavailable
        except Exception as exc:  # noqa: BLE001
            logger.warning("slash command failed, fallback to agent: %s", exc)
            # 非斜杠或处理失败，继续走 Agent

    ipc = FeishuIPCBridge(
        ipc_timeout_seconds=feishu_cfg.automation_ipc_timeout_seconds,
    )
    try:
        result = await ipc.send_message(
            session_id=session_id,
            text=text,
            metadata=metadata,
            owner_id="root",
            source="feishu",
        )
    except AutomationDaemonUnavailable as exc:
        logger.warning("automation daemon unavailable for feishu ws message: %s", exc)
        await send_feishu_error_notice(
            chat_id=feishu_message.chat_id,
            text=MSG_FEISHU_DAEMON_UNAVAILABLE,
            timeout_seconds=feishu_cfg.timeout_seconds,
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "failed to process feishu ws message via automation daemon: %s", exc
        )
        await send_feishu_error_notice(
            chat_id=feishu_message.chat_id,
            text=format_feishu_processing_error(exc),
            timeout_seconds=feishu_cfg.timeout_seconds,
        )
        return

    # 将 Agent 回复发回飞书（已在 IPC 内对流式 PATCH 最终标题时跳过重复发送）
    feishu_client = FeishuClient(timeout_seconds=feishu_cfg.timeout_seconds)
    try:
        if not (result.metadata or {}).get("feishu_skip_final_reply"):
            await send_feishu_agent_final_reply(
                client=feishu_client,
                chat_id=feishu_message.chat_id,
                output_text=result.output_text,
            )
        attachments = getattr(result, "attachments", None)
        if attachments:
            await feishu_client.send_reply_attachments(
                chat_id=feishu_message.chat_id,
                attachments=attachments,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to send feishu ws reply: %s", exc)

    # 注册 push 转发：subagent 完成等 inject_turn 结果将经 [out] 队列推送到本会话
    chat_id = feishu_message.chat_id
    if chat_id:
        register_feishu_push_session(
            session_id=session_id,
            chat_id=chat_id,
            ttl_seconds=300.0,
        )


def _run_handler_in_thread(data: Any) -> None:
    """在独立线程中运行异步事件处理，避免与 SDK 内部事件循环冲突。"""
    try:
        asyncio.run(_handle_im_message_event_async(data))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Feishu ws handler thread failed: %s", exc)


def _handle_im_message_event(data: Any) -> None:
    """
    lark-oapi 事件分发器期望的同步回调。

    这里启动一个 daemon 线程，在线程内创建事件循环并执行异步处理逻辑，
    避免在 SDK 所在线程内嵌套/干扰其内部事件循环。
    """
    t = threading.Thread(target=_run_handler_in_thread, args=(data,), daemon=True)
    t.start()


def build_ws_client() -> lark.ws.Client:
    """
    构建飞书长连接客户端。

    Returns:
        已配置事件分发器的 lark.ws.Client 实例
    """
    cfg = get_config()
    feishu_cfg = cfg.feishu
    if not feishu_cfg.enabled:
        raise RuntimeError("feishu.enabled=false，无法启动飞书长连接客户端")
    if not (feishu_cfg.app_id and feishu_cfg.app_secret):
        raise RuntimeError("Feishu app_id/app_secret 未配置，无法启动长连接客户端")

    # 使用 v2 事件分发器：IM 消息 + 卡片按钮回传（须同时注册，否则 card.action.trigger 报 processor not found）
    event_handler = (
        lark.EventDispatcherHandler.builder(
            feishu_cfg.verification_token or "",
            feishu_cfg.encrypt_key or "",
        )
        .register_p2_im_message_receive_v1(_handle_im_message_event)
        .register_p2_card_action_trigger(_handle_p2_card_action_trigger)
        .build()
    )

    client = lark.ws.Client(
        feishu_cfg.app_id,
        feishu_cfg.app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    return client


def run_ws_client() -> None:
    """启动飞书长连接客户端（阻塞调用）。"""
    get_feishu_push_forwarder().start()
    client = build_ws_client()
    logger.info("Starting Feishu long-connection client...")
    client.start()
