"""处理飞书 card.action.trigger（卡片按钮回传）。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from fastapi.responses import JSONResponse

from agent_core.permissions.wait_registry import PermissionDecision, resolve_permission

from agent_core.config import get_config
from system.automation.ipc import AutomationIPCClient, default_socket_path

from .config import get_feishu_config
from .ask_user_card import (
    ASK_CUSTOM,
    ASK_PICK,
    ASK_USER_VALUE_KEY,
    merge_ask_user_action_value,
)
from .permission_card import (
    ALLOW,
    CLARIFY,
    DENY,
    build_permission_request_card,
    parse_permission_card_payload,
)
from .remote_login_card import (APPROVE, REJECT, extract_card_operator_ids,
                                parse_remote_login_card_payload)

logger = logging.getLogger(__name__)


def _resolved_card_from_value(
    raw_val: Any,
    form_value: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    parsed = parse_permission_card_payload(raw_val, form_value)
    pid = parsed["permission_id"]
    dec = parsed["decision"]
    if not pid or dec not in (ALLOW, DENY, CLARIFY):
        return None
    summary = parsed["summary_echo"] or "（无摘要存档）"
    rp = parsed["persist_acl"] if dec == ALLOW else None
    return build_permission_request_card(
        permission_id=pid,
        summary=summary,
        kind=parsed["kind_echo"],
        timeout_seconds=parsed["timeout_echo"],
        path_prefix=parsed["path_prefix"],
        tool_name=parsed["tool_name"],
        command=parsed["command"],
        cwd=parsed["cwd"],
        risk_reasons=parsed["risk_reasons"],
        path_grants=parsed["path_grants"],
        auto_execute_after_approval=parsed["auto_execute_after_approval"],
        resolved=dec,  # type: ignore[arg-type]
        resolved_user_instruction=(
            parsed["user_instruction"] if dec == CLARIFY else None
        ),
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
    parsed = parse_permission_card_payload(raw_val, form_value)
    pid = parsed["permission_id"]
    dec = parsed["decision"]
    pfx = parsed["path_prefix"]
    ui = parsed["user_instruction"]
    persist_acl = parsed["persist_acl"]
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
                path_grants=parsed["path_grants"],
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


async def resolve_ask_user_via_daemon_ipc(
    raw_val: Any,
    *,
    form_value: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """通过 Automation IPC 在 daemon 内执行 submit_ask_user_fragment。"""
    merged = merge_ask_user_action_value(raw_val, form_value)
    mode = str(merged.get(ASK_USER_VALUE_KEY) or "").strip().lower()
    if mode not in (ASK_PICK, ASK_CUSTOM):
        return "warning", "无法识别的 ask_user 操作", None

    bid = str(merged.get("batch_id") or merged.get("ask_user_id") or "").strip()
    qid = str(merged.get("question_id") or "").strip()
    if not bid or not qid:
        logger.warning("ask_user card: missing batch_id/question_id raw=%r", raw_val)
        return "warning", "无法识别本题", None

    selected_opt: Optional[str] = None
    custom_txt: Optional[str] = None
    if mode == ASK_PICK:
        so = merged.get("selected_option")
        selected_opt = str(so).strip() if so is not None and str(so).strip() else None
        if not selected_opt:
            return "warning", "请点选选项", None
    else:
        ct = merged.get("custom_text")
        custom_txt = str(ct).strip() if ct is not None and str(ct).strip() else None
        if not custom_txt:
            return "warning", "请填写说明后再提交", None

    cfg = get_config()
    timeout = min(float(cfg.llm.request_timeout_seconds or 120.0), 60.0)
    ipc = AutomationIPCClient(
        owner_id="root",
        source="feishu",
        socket_path=default_socket_path(),
        timeout_seconds=timeout,
    )
    if not await ipc.ping():
        logger.error("card ask_user: automation_daemon IPC unreachable")
        return "error", "无法连接 automation_daemon，请确认已启动", None

    ok, detail, card_dict = await ipc.submit_ask_user_fragment(
        batch_id=bid,
        question_id=qid,
        selected_option=selected_opt,
        custom_text=custom_txt,
    )
    if not ok:
        if detail in ("unknown_batch", "already_resolved"):
            return "warning", "该提问已处理或已超时", None
        return "warning", detail or "提交失败", None

    if detail == "completed":
        return "success", "已提交，Agent 将继续处理", card_dict
    if detail.startswith("partial:"):
        return "success", "已记录，请继续选择其余题目", card_dict
    return "success", "已记录", card_dict


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
    parsed = parse_permission_card_payload(raw_val, form_value)
    pid = parsed["permission_id"]
    dec = parsed["decision"]
    pfx = parsed["path_prefix"]
    ui = parsed["user_instruction"]
    persist_acl = parsed["persist_acl"]
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
        path_grants=parsed["path_grants"] if allowed else None,
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


async def resolve_remote_login_via_daemon_ipc(
    *,
    request_id: str,
    approve: bool,
    approver_open_id: str = "",
    approver_user_id: str = "",
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """通过 Automation IPC 在 daemon 内执行远程登录卡片审批。"""
    rid = (request_id or "").strip()
    if not rid:
        return "warning", "缺少 request_id", None

    cfg = get_config()
    timeout = min(float(cfg.llm.request_timeout_seconds or 120.0), 60.0)
    ipc = AutomationIPCClient(
        owner_id="root",
        source="feishu",
        socket_path=default_socket_path(),
        timeout_seconds=timeout,
    )
    if not await ipc.ping():
        logger.error("card remote_login: automation_daemon IPC unreachable")
        return "error", "无法连接 automation_daemon，请确认已启动", None

    try:
        kind, msg, card_dict = await ipc.resolve_remote_login_feishu(
            request_id=rid,
            approve=approve,
            approver_open_id=approver_open_id,
            approver_user_id=approver_user_id,
        )
    except RuntimeError as exc:
        logger.warning("resolve_remote_login_feishu ipc failed: %s", exc)
        return "error", str(exc) or "IPC 调用失败", None

    return kind, msg, card_dict


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

    merged_au = merge_ask_user_action_value(raw_val, form_value)
    if str(merged_au.get(ASK_USER_VALUE_KEY) or "").strip() in (ASK_PICK, ASK_CUSTOM):
        kind, msg, card_dict = await resolve_ask_user_via_daemon_ipc(
            raw_val, form_value=form_value
        )
        return _toast_response(msg=msg, kind=kind, card=card_dict)

    parsed_remote = parse_remote_login_card_payload(raw_val)
    if parsed_remote.get("decision") in (APPROVE, REJECT):
        open_id, user_id = extract_card_operator_ids(body)
        kind, msg, card_dict = await resolve_remote_login_via_daemon_ipc(
            request_id=parsed_remote.get("request_id") or "",
            approve=parsed_remote.get("decision") == APPROVE,
            approver_open_id=open_id,
            approver_user_id=user_id,
        )
        return _toast_response(msg=msg, kind=kind, card=card_dict)

    kind, msg, card_dict = await resolve_card_via_daemon_ipc(
        raw_val, form_value=form_value
    )
    return _toast_response(msg=msg, kind=kind, card=card_dict)
