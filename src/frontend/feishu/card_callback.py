"""处理飞书 card.action.trigger（卡片按钮回传）。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from fastapi.responses import JSONResponse

from agent_core.permissions.wait_registry import PermissionDecision, resolve_permission

from agent_core.config import get_config
from system.automation.ipc import AutomationIPCClient, default_socket_path

from .config import get_feishu_config
from .permission_card import DENY, ALLOW, parse_card_action_value

logger = logging.getLogger(__name__)


def execute_card_permission_resolution(raw_val: Any) -> Tuple[str, str]:
    """
    在**当前进程**内解析并 resolve_permission（仅单测或与 daemon 同进程时使用）。

    飞书网关进程须使用 :func:`resolve_card_via_daemon_ipc`。
    """
    pid, dec, pfx = parse_card_action_value(raw_val)
    if not pid or dec not in (ALLOW, DENY):
        logger.warning(
            "card.action.trigger: bad value pid=%r dec=%r raw=%r",
            pid,
            dec,
            raw_val,
        )
        return "warning", "无法识别该操作"

    allowed = dec == ALLOW
    ok = resolve_permission(
        pid,
        PermissionDecision(
            allowed=allowed,
            path_prefix=pfx if allowed and pfx else None,
            note="飞书卡片按钮" + ("批准" if allowed else "拒绝"),
        ),
    )
    if not ok:
        logger.info(
            "resolve_permission failed (unknown or resolved) permission_id=%s", pid
        )
        return "warning", "该申请已处理或已过期"

    return "success", "已批准" if allowed else "已拒绝"


async def resolve_card_via_daemon_ipc(raw_val: Any) -> Tuple[str, str]:
    """
    通过 Automation IPC 在 automation_daemon 进程内执行 resolve_permission。

    request_permission 注册的 Future 仅存在于 daemon 内，网关进程不可直接 resolve_permission。
    """
    pid, dec, pfx = parse_card_action_value(raw_val)
    if not pid or dec not in (ALLOW, DENY):
        logger.warning(
            "card.action.trigger: bad value pid=%r dec=%r raw=%r",
            pid,
            dec,
            raw_val,
        )
        return "warning", "无法识别该操作"

    allowed = dec == ALLOW
    note = "飞书卡片按钮" + ("批准" if allowed else "拒绝")
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
        return "error", "无法连接 automation_daemon，请确认已启动"

    ok = await ipc.resolve_permission(
        permission_id=pid,
        allowed=allowed,
        path_prefix=pfx if allowed and pfx else None,
        note=note,
    )
    if not ok:
        logger.info(
            "daemon resolve_permission failed permission_id=%s (unknown or resolved)",
            pid,
        )
        return "warning", "该申请已处理或已过期"

    return "success", "已批准" if allowed else "已拒绝"


def _toast_response(
    *, msg: str, kind: str = "success"
) -> JSONResponse:
    # 新版卡片回调响应：https://open.feishu.cn/document/feishu-cards/card-callback-communication
    return JSONResponse(
        content={
            "toast": {
                "type": kind,
                "content": msg,
            }
        }
    )


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
    kind, msg = await resolve_card_via_daemon_ipc(raw_val)
    return _toast_response(msg=msg, kind=kind)
