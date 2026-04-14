"""处理飞书 card.action.trigger（卡片按钮回传）。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from fastapi.responses import JSONResponse

from agent_core.permissions.wait_registry import PermissionDecision, resolve_permission

from agent_core.config import get_config
from system.automation.ipc import AutomationIPCClient, default_socket_path

from .config import get_feishu_config
from .permission_card import (
    ALLOW,
    CLARIFY,
    DENY,
    build_permission_request_card,
    parse_permission_card_callback,
)

logger = logging.getLogger(__name__)


def _resolved_card_from_value(
    raw_val: Any,
    form_value: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    (
        pid,
        dec,
        pfx,
        sum_e,
        kind_e,
        timeout_echo,
        ui,
        persist_acl,
    ) = parse_permission_card_callback(raw_val, form_value)
    if not pid or dec not in (ALLOW, DENY, CLARIFY):
        return None
    summary = sum_e or "（无摘要存档）"
    rp = persist_acl if dec == ALLOW else None
    return build_permission_request_card(
        permission_id=pid,
        summary=summary,
        kind=kind_e,
        timeout_seconds=timeout_echo,
        path_prefix=pfx,
        resolved=dec,  # type: ignore[arg-type]
        resolved_user_instruction=ui if dec == CLARIFY else None,
        resolved_persist_acl=rp,
    )


def execute_card_permission_resolution(
    raw_val: Any,
    form_value: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """
    在**当前进程**内解析并 resolve_permission（仅单测或与 daemon 同进程时使用）。

    飞书网关进程须使用 :func:`resolve_card_via_daemon_ipc`。
    """
    (
        pid,
        dec,
        pfx,
        sum_e,
        kind_e,
        _timeout_echo,
        ui,
        persist_acl,
    ) = parse_permission_card_callback(raw_val, form_value)
    if not pid or dec not in (ALLOW, DENY, CLARIFY):
        logger.warning(
            "card.action.trigger: bad value pid=%r dec=%r raw=%r",
            pid,
            dec,
            raw_val,
        )
        return "warning", "无法识别该操作", None

    if dec == CLARIFY:
        ok = resolve_permission(
            pid,
            PermissionDecision(
                allowed=False,
                path_prefix=None,
                note="飞书卡片：用户提交精确说明（未批准权限）",
                clarify_requested=True,
                user_instruction=ui,
            ),
        )
    elif dec == ALLOW:
        ok = resolve_permission(
            pid,
            PermissionDecision(
                allowed=True,
                path_prefix=pfx if pfx else None,
                note="飞书卡片批准",
                persist_acl=persist_acl,
            ),
        )
    else:
        ok = resolve_permission(
            pid,
            PermissionDecision(
                allowed=False,
                note="飞书卡片按钮拒绝",
            ),
        )

    if not ok:
        logger.info(
            "resolve_permission failed (unknown or resolved) permission_id=%s", pid
        )
        return "warning", "该申请已处理或已过期", None

    card_dict = _resolved_card_from_value(raw_val, form_value)
    if dec == CLARIFY:
        return "success", "已提交说明并回传给 Agent", card_dict
    return "success", "已批准" if dec == ALLOW else "已拒绝", card_dict


async def resolve_card_via_daemon_ipc(
    raw_val: Any,
    *,
    form_value: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """
    通过 Automation IPC 在 automation_daemon 进程内执行 resolve_permission。

    form_value：表单提交时 event.action.form_value（输入框内容）。
    第三元组为成功时用于更新卡片的 JSON 2.0 对象；失败时为 None。
    """
    (
        pid,
        dec,
        pfx,
        _sum_e,
        _kind_e,
        _timeout_echo,
        ui,
        persist_acl,
    ) = parse_permission_card_callback(raw_val, form_value)
    if not pid or dec not in (ALLOW, DENY, CLARIFY):
        logger.warning(
            "card.action.trigger: bad value pid=%r dec=%r raw=%r",
            pid,
            dec,
            raw_val,
        )
        return "warning", "无法识别该操作", None

    clarify_requested = dec == CLARIFY
    allowed = dec == ALLOW
    note = (
        "飞书卡片：用户提交精确说明（未批准权限）"
        if clarify_requested
        else ("飞书卡片批准" if allowed else "飞书卡片按钮拒绝")
    )

    cfg = get_config()
    timeout = min(float(cfg.llm.request_timeout_seconds or 120.0), 60.0)
    ipc = AutomationIPCClient(
        owner_id="root",
        source="feishu",
        socket_path=default_socket_path(),
        timeout_seconds=timeout,
    )
    if not await ipc.ping():
        logger.error("card permission: automation_daemon IPC unreachable")
        return "error", "无法连接 automation_daemon，请确认已启动", None

    ok = await ipc.resolve_permission(
        permission_id=pid,
        allowed=allowed,
        path_prefix=pfx if allowed and pfx else None,
        note=note,
        clarify_requested=clarify_requested,
        user_instruction=ui if clarify_requested else None,
        persist_acl=persist_acl if allowed else False,
    )
    if not ok:
        logger.info(
            "daemon resolve_permission failed permission_id=%s (unknown or resolved)",
            pid,
        )
        return "warning", "该申请已处理或已过期", None

    card_dict = _resolved_card_from_value(raw_val, form_value)
    if clarify_requested:
        return "success", "已提交说明并回传给 Agent", card_dict
    return "success", "已批准" if allowed else "已拒绝", card_dict


def _toast_response(
    *,
    msg: str,
    kind: str = "success",
    card: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    # 新版卡片回调响应：https://open.feishu.cn/document/feishu-cards/card-callback-communication
    content: Dict[str, Any] = {
        "toast": {
            "type": kind,
            "content": msg,
        }
    }
    if card is not None:
        content["card"] = {"type": "raw", "data": card}
    return JSONResponse(content=content)


async def handle_feishu_card_action(body: Dict[str, Any]) -> JSONResponse:
    """
    处理 schema 2.0 的 card.action.trigger 请求体。

    需在飞书开发者后台「事件与回调」中订阅「卡片回传交互」，回调 URL 与 im 消息一致。
    """
    cfg = get_feishu_config()
    header = body.get("header") or {}
    if (
        cfg.verification_token
        and header.get("token")
        and header.get("token") != cfg.verification_token
    ):
        # 须 HTTP 200，否则客户端报 200671
        return JSONResponse(
            status_code=200,
            content={
                "toast": {
                    "type": "error",
                    "content": "verification_token 与开放平台配置不一致",
                }
            },
        )

    event = body.get("event") or {}
    action = event.get("action") or {}
    raw_val = action.get("value")
    form_value = action.get("form_value")
    if form_value is not None and not isinstance(form_value, dict):
        form_value = None

    kind, msg, card_dict = await resolve_card_via_daemon_ipc(
        raw_val, form_value=form_value
    )
    return _toast_response(msg=msg, kind=kind, card=card_dict)
