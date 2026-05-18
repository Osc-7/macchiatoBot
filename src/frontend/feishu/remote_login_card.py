"""飞书交互卡片：远程 worker 首次登录审批。"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

VALUE_KEY = "macchiato_remote_login"
APPROVE = "approve"
REJECT = "reject"


def _coerce_raw_mapping(raw_val: Any, *, depth: int = 0) -> Dict[str, Any]:
    if depth > 4:
        return {}
    if isinstance(raw_val, dict):
        data = raw_val
    elif isinstance(raw_val, str) and raw_val.strip():
        try:
            parsed = json.loads(raw_val)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        data = parsed
    else:
        return {}

    if VALUE_KEY in data or "decision" in data or "request_id" in data:
        return data

    for key in ("value", "data", "payload", "action_value"):
        if key in data:
            nested = _coerce_raw_mapping(data.get(key), depth=depth + 1)
            if nested:
                return nested
    return data


def parse_remote_login_card_payload(raw_val: Any) -> Dict[str, str]:
    data = _coerce_raw_mapping(raw_val)
    request_id = str(data.get("request_id") or "").strip()
    decision = str(data.get(VALUE_KEY) or data.get("decision") or "").strip().lower()
    if decision not in {APPROVE, REJECT}:
        decision = ""
    login = str(data.get("login") or "").strip()
    device_name = str(data.get("device_name") or "").strip()
    requester_ip = str(data.get("requester_ip") or "").strip()
    created_at = str(data.get("created_at") or "").strip()
    return {
        "request_id": request_id,
        "decision": decision,
        "login": login,
        "device_name": device_name,
        "requester_ip": requester_ip,
        "created_at": created_at,
    }


def build_remote_login_request_card(
    *,
    request_id: str,
    login: str,
    device_name: str,
    requester_ip: str,
    created_at: str,
    resolved: Optional[str] = None,
    approver_label: str = "",
) -> Dict[str, Any]:
    rid = (request_id or "").strip()
    login_s = (login or "").strip() or "-"
    device_s = (device_name or "").strip() or "-"
    ip_s = (requester_ip or "").strip() or "-"
    created_s = (created_at or "").strip() or "-"

    status = "Pending"
    template = "orange"
    if resolved == APPROVE:
        status = "Approved"
        template = "green"
    elif resolved == REJECT:
        status = "Rejected"
        template = "carmine"

    md_lines = [
        f"**Remote worker login request**",
        "",
        f"- login: `{login_s}`",
        f"- device: `{device_s}`",
        f"- source ip: `{ip_s}`",
        f"- request id: `{rid}`",
        f"- created at: `{created_s}`",
    ]
    if approver_label:
        md_lines.append(f"- approver: `{approver_label}`")

    elements: list[Dict[str, Any]] = [
        {"tag": "markdown", "content": "\n".join(md_lines), "text_align": "left"},
    ]
    if resolved not in {APPROVE, REJECT}:
        base_value = {
            "request_id": rid,
            "login": login_s,
            "device_name": device_s,
            "requester_ip": ip_s,
            "created_at": created_s,
        }
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "none",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "button",
                                "type": "primary",
                                "text": {"tag": "plain_text", "content": "Approve"},
                                "behaviors": [
                                    {
                                        "type": "callback",
                                        "value": {
                                            **base_value,
                                            VALUE_KEY: APPROVE,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "button",
                                "type": "danger",
                                "text": {"tag": "plain_text", "content": "Reject"},
                                "behaviors": [
                                    {
                                        "type": "callback",
                                        "value": {
                                            **base_value,
                                            VALUE_KEY: REJECT,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        )

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": "Remote Login Approval"},
            "subtitle": {"tag": "plain_text", "content": f"Status: {status}"},
        },
        "body": {
            "direction": "vertical",
            "padding": "8px 16px 16px 16px",
            "vertical_spacing": "medium",
            "elements": elements,
        },
    }


def extract_card_operator_ids(body: Dict[str, Any]) -> Tuple[str, str]:
    event = body.get("event") or {}
    operator = event.get("operator") or {}
    op_id = operator.get("operator_id") or {}
    open_id = str(
        operator.get("open_id")
        or op_id.get("open_id")
        or event.get("open_id")
        or ""
    ).strip()
    user_id = str(
        operator.get("user_id")
        or op_id.get("user_id")
        or event.get("user_id")
        or ""
    ).strip()
    return open_id, user_id
