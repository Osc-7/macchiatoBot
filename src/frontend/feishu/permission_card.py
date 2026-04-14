"""飞书交互卡片：权限申请（回传 card.action.trigger）。

UI 文案为英文，风格对齐常见 CLI（Codex/bash）：Once / Always / Deny；可选 Note 仅反馈、不授权。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

# 与回调解析一致（card_callback.py）
VALUE_KEY = "macchiato_permission"
ALLOW = "allow"
DENY = "deny"
CLARIFY = "clarify"

# 表单内输入框 name（单卡唯一）；回调在 event.action.form_value[name] 中
USER_INSTRUCTION_FIELD = "macchiato_user_instruction"

ResolvedDecision = Optional[Literal["allow", "deny", "clarify"]]


def _truncate(s: str, max_len: int) -> str:
    t = (s or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _element_suffix(permission_id: str) -> str:
    h = re.sub(r"[^0-9a-fA-F]", "", permission_id)[:8]
    if len(h) < 8:
        h = (h + "00000000")[:8]
    return h.lower()


def _header_for_resolved(
    resolved: ResolvedDecision,
    *,
    allow_persist: Optional[bool] = None,
) -> Tuple[str, str, str]:
    """template, title, subtitle（Codex-style: Once / Always / Deny）。"""
    if resolved == ALLOW:
        if allow_persist is True:
            return "green", "Permission", "Always"
        if allow_persist is False:
            return "green", "Permission", "Once"
        return "green", "Permission", "Approved"
    if resolved == DENY:
        return "carmine", "Permission", "Denied"
    if resolved == CLARIFY:
        return "blue", "Permission", "Clarify"
    return "orange", "Permission", "Pending"


def merge_action_value_with_form(
    raw_val: Any,
    form_value: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    合并按钮 behaviors.value 与表单提交时的 form_value（输入框内容在 form_value[name]）。
    """
    merged: Dict[str, Any]
    if isinstance(raw_val, dict):
        merged = dict(raw_val)
    elif isinstance(raw_val, str) and raw_val.strip():
        try:
            merged = dict(json.loads(raw_val))
        except json.JSONDecodeError:
            merged = {}
    else:
        merged = {}
    if form_value and isinstance(form_value, dict):
        if USER_INSTRUCTION_FIELD in form_value:
            merged["user_instruction"] = form_value[USER_INSTRUCTION_FIELD]
    return merged


def _parse_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


def parse_permission_card_callback(
    raw_val: Any,
    form_value: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, Optional[str], str, str, Optional[float], str, bool]:
    """
    解析权限卡片交互（含表单提交的 user_instruction）。

    返回：
    (permission_id, decision, path_prefix, summary_echo, kind_echo, timeout_echo,
     user_instruction, persist_acl)

    persist_acl 仅在 decision==allow 时有意义：来自**人类点击**的按钮（「加白名单」为 True，「本次有效」为 False），非 Agent 生成。
    """
    data = merge_action_value_with_form(raw_val, form_value)

    pid = str(data.get("permission_id") or "").strip()
    dec = str(data.get(VALUE_KEY) or data.get("decision") or "").strip().lower()
    if dec in ("approve", "yes", "ok", "y"):
        dec = ALLOW
    if dec in ("reject", "no", "n"):
        dec = DENY
    pfx = str(data.get("path_prefix") or "").strip() or None
    sum_e = str(data.get("summary_echo") or "").strip()
    kind_e = str(data.get("kind_echo") or "").strip()
    timeout_echo: Optional[float] = None
    if data.get("timeout_echo") is not None:
        try:
            timeout_echo = float(data["timeout_echo"])
        except (TypeError, ValueError):
            timeout_echo = None
    ui = data.get("user_instruction")
    user_instruction = str(ui).strip() if ui is not None else ""
    persist_acl = _parse_bool(data.get("persist_acl"), default=False)
    if dec != ALLOW:
        persist_acl = False
    return pid, dec, pfx, sum_e, kind_e, timeout_echo, user_instruction, persist_acl


def parse_card_action_value(
    raw: Any,
) -> Tuple[str, str, Optional[str], str, str, Optional[float], str, bool]:
    """向后兼容：仅从 value 解析（无表单）。"""
    return parse_permission_card_callback(raw, None)


def build_permission_request_card(
    *,
    permission_id: str,
    summary: str,
    kind: str = "",
    timeout_seconds: float | None = None,
    path_prefix: Optional[str] = None,
    resolved: ResolvedDecision = None,
    resolved_user_instruction: Optional[str] = None,
    resolved_persist_acl: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    构建卡片 JSON 2.0（可直接作为 im/v1/messages interactive 的 content 对象）。

    resolved 非空时：展示「当前选择」且不再渲染操作区（用于回调后更新卡片）。

    表单容器须作为 body.elements 的直接子节点（飞书限制），内嵌输入框 + 提交按钮，
    提交时 instruction 在回调 event.action.form_value 中。
    """
    suf = _element_suffix(permission_id)
    summary_t = _truncate(summary, 900)
    kind_t = _truncate(kind, 80)
    timeout_s = ""
    if timeout_seconds is not None:
        try:
            timeout_s = f"{int(float(timeout_seconds))}s"
        except (TypeError, ValueError):
            timeout_s = ""

    md_lines = [f"**{summary_t}**"]
    if kind_t:
        md_lines.append(f"`{kind_t}`")
    if timeout_s:
        md_lines.append(f"`{timeout_s}`")
    if path_prefix:
        md_lines.append(f"`{path_prefix}`")
    md_lines.append(f"`{permission_id}`")

    if resolved == ALLOW:
        if resolved_persist_acl is True:
            md_lines.append("✅ **Always**")
        elif resolved_persist_acl is False:
            md_lines.append("✅ **Once**")
        else:
            md_lines.append("✅ Approved")
    elif resolved == DENY:
        md_lines.append("❌ **Denied**")
    elif resolved == CLARIFY:
        ins = (resolved_user_instruction or "").strip()
        if ins:
            safe = ins.replace("\n", "\n> ")
            md_lines.append(f"💬 **Clarify**\n> {safe}")
        else:
            md_lines.append("💬 **Clarify** (empty)")

    md_content = "\n".join(md_lines)

    tpl, title, subtitle = _header_for_resolved(
        resolved,
        allow_persist=resolved_persist_acl if resolved == ALLOW else None,
    )
    summary_preview = _truncate(summary_t, 100)

    elements: list[Dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": md_content,
            "text_align": "left",
            "text_size": "normal_v2",
            "margin": "0px 0px 8px 0px",
        }
    ]

    if resolved is None:
        echo_summary = summary_t
        echo_kind = kind_t

        def _base_allow(persist_acl: bool) -> Dict[str, Any]:
            # persist_acl 仅嵌入卡片按钮 value，由人类点击回传；LLM 无法写入此 JSON。
            d: Dict[str, Any] = {
                VALUE_KEY: ALLOW,
                "permission_id": permission_id,
                "summary_echo": echo_summary,
                "kind_echo": echo_kind,
                "persist_acl": persist_acl,
            }
            if path_prefix:
                d["path_prefix"] = path_prefix
            if timeout_seconds is not None:
                try:
                    d["timeout_echo"] = float(timeout_seconds)
                except (TypeError, ValueError):
                    pass
            return d

        value_allow_once = _base_allow(False)
        value_allow_acl = _base_allow(True)
        value_deny: Dict[str, Any] = {
            VALUE_KEY: DENY,
            "permission_id": permission_id,
            "summary_echo": echo_summary,
            "kind_echo": echo_kind,
        }
        value_clarify: Dict[str, Any] = {
            VALUE_KEY: CLARIFY,
            "permission_id": permission_id,
            "summary_echo": echo_summary,
            "kind_echo": echo_kind,
            "persist_acl": False,
        }
        if path_prefix:
            value_deny["path_prefix"] = path_prefix
            value_clarify["path_prefix"] = path_prefix
        if timeout_seconds is not None:
            try:
                te = float(timeout_seconds)
                value_deny["timeout_echo"] = te
                value_clarify["timeout_echo"] = te
            except (TypeError, ValueError):
                pass

        if path_prefix:
            approve_columns = [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "elements": [
                        {
                            "tag": "button",
                            "element_id": f"a1_{suf}",
                            "type": "primary_filled",
                            "size": "medium",
                            "width": "fill",
                            "text": {
                                "tag": "plain_text",
                                "content": "Once",
                            },
                            "behaviors": [
                                {"type": "callback", "value": value_allow_once}
                            ],
                        }
                    ],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "elements": [
                        {
                            "tag": "button",
                            "element_id": f"a2_{suf}",
                            "type": "primary_filled",
                            "size": "medium",
                            "width": "fill",
                            "text": {
                                "tag": "plain_text",
                                "content": "Always",
                            },
                            "behaviors": [
                                {"type": "callback", "value": value_allow_acl}
                            ],
                        }
                    ],
                },
            ]
        else:
            approve_columns = [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "elements": [
                        {
                            "tag": "button",
                            "element_id": f"a_{suf}",
                            "type": "primary_filled",
                            "size": "medium",
                            "width": "fill",
                            "text": {
                                "tag": "plain_text",
                                "content": "Allow",
                            },
                            "behaviors": [
                                {"type": "callback", "value": value_allow_once}
                            ],
                        }
                    ],
                },
            ]

        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "flow",
                "background_style": "default",
                "columns": approve_columns
                + [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "vertical_align": "top",
                        "elements": [
                            {
                                "tag": "button",
                                "element_id": f"d_{suf}",
                                "type": "danger_filled",
                                "size": "medium",
                                "width": "fill",
                                "text": {
                                    "tag": "plain_text",
                                    "content": "Deny",
                                },
                                "behaviors": [
                                    {
                                        "type": "callback",
                                        "value": value_deny,
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        )

        # 表单容器：仅可放在 body.elements 根级（与 markdown、column_set 并列）
        elements.append(
            {
                "tag": "form",
                "name": f"form_macchiato_{suf}",
                "element_id": f"fm_{suf}",
                "direction": "vertical",
                "vertical_spacing": "medium",
                "elements": [
                    {
                        "tag": "input",
                        "name": USER_INSTRUCTION_FIELD,
                        "input_type": "multiline_text",
                        "rows": 3,
                        "max_length": 1000,
                        "width": "fill",
                        "required": False,
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "Optional note (does not grant)",
                        },
                        "label": {
                            "tag": "plain_text",
                            "content": "Note",
                        },
                        "label_position": "top",
                        "fallback": {
                            "tag": "fallback_text",
                            "text": {
                                "tag": "plain_text",
                                "content": "Feishu V6.8+ required for text input",
                            },
                        },
                    },
                    {
                        "tag": "column_set",
                        "flex_mode": "none",
                        "horizontal_align": "left",
                        "columns": [
                            {
                                "tag": "column",
                                "width": "auto",
                                "vertical_align": "top",
                                "elements": [
                                    {
                                        "tag": "button",
                                        "name": f"btn_clarify_{suf}",
                                        "type": "primary",
                                        "size": "medium",
                                        "text": {
                                            "tag": "plain_text",
                                            "content": "Submit",
                                        },
                                        "form_action_type": "submit",
                                        "behaviors": [
                                            {
                                                "type": "callback",
                                                "value": value_clarify,
                                            }
                                        ],
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
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "summary": {"content": summary_preview},
        },
        "header": {
            "template": tpl,
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {
                "tag": "plain_text",
                "content": subtitle,
            },
            "padding": "12px 14px 12px 14px",
        },
        "body": {
            "direction": "vertical",
            "padding": "8px 16px 16px 16px",
            "vertical_spacing": "medium",
            "elements": elements,
        },
    }


def interactive_content_string(card: Dict[str, Any]) -> str:
    """与飞书「方式三：使用卡片 JSON 发送」一致，整卡序列化为字符串。"""
    return json.dumps(card, ensure_ascii=False)
