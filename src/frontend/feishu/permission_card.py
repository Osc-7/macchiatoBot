"""飞书交互卡片：权限申请（批准 / 拒绝 / 表单提交精确说明，回传 card.action.trigger）。"""

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


def _header_for_resolved(resolved: ResolvedDecision) -> Tuple[str, str, str]:
    """template, title, subtitle"""
    if resolved == ALLOW:
        return "green", "权限申请（已处理）", "已批准"
    if resolved == DENY:
        return "carmine", "权限申请（已处理）", "已拒绝"
    if resolved == CLARIFY:
        return "blue", "权限申请（已处理）", "已提交说明"
    return "orange", "权限申请", "Policy · approval"


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


def parse_permission_card_callback(
    raw_val: Any,
    form_value: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, Optional[str], str, str, Optional[float], str]:
    """
    解析权限卡片交互（含表单提交的 user_instruction）。

    返回：
    (permission_id, decision, path_prefix, summary_echo, kind_echo, timeout_echo, user_instruction)

    decision 为 allow | deny | clarify；未提交说明时 user_instruction 为空字符串。
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
    return pid, dec, pfx, sum_e, kind_e, timeout_echo, user_instruction


def parse_card_action_value(
    raw: Any,
) -> Tuple[str, str, Optional[str], str, str, Optional[float], str]:
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
    timeout_line = ""
    if timeout_seconds is not None:
        try:
            timeout_line = f"\n- 等待超时（秒）：{int(float(timeout_seconds))}"
        except (TypeError, ValueError):
            timeout_line = ""

    md_lines = [
        "**权限申请**",
        f"- 摘要：{summary_t}",
    ]
    if kind_t:
        md_lines.append(f"- 类别：{kind_t}")
    if timeout_line:
        md_lines.append(timeout_line.strip())
    md_lines.append(f"- `permission_id`：`{permission_id}`")
    if path_prefix:
        md_lines.append(f"- 批准后写入白名单前缀：`{path_prefix}`")

    if resolved == ALLOW:
        md_lines.append("**当前选择**：✅ 已批准")
    elif resolved == DENY:
        md_lines.append("**当前选择**：❌ 已拒绝")
    elif resolved == CLARIFY:
        md_lines.append("**当前选择**：💬 已通过表单提交说明（**本次未批准权限**）")
        ins = (resolved_user_instruction or "").strip()
        if ins:
            # 避免破坏 markdown：缩进引用块
            safe = ins.replace("\n", "\n> ")
            md_lines.append(f"\n**用户说明**：\n> {safe}")
        else:
            md_lines.append("\n**用户说明**：（未填写）")

    md_content = "\n".join(md_lines)

    tpl, title, subtitle = _header_for_resolved(resolved)
    summary_preview = _truncate(f"权限 · {summary_t}", 100)

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
        value_allow: Dict[str, Any] = {
            VALUE_KEY: ALLOW,
            "permission_id": permission_id,
            "summary_echo": echo_summary,
            "kind_echo": echo_kind,
        }
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
        }
        if path_prefix:
            value_allow["path_prefix"] = path_prefix
            value_deny["path_prefix"] = path_prefix
            value_clarify["path_prefix"] = path_prefix
        if timeout_seconds is not None:
            try:
                te = float(timeout_seconds)
                value_allow["timeout_echo"] = te
                value_deny["timeout_echo"] = te
                value_clarify["timeout_echo"] = te
            except (TypeError, ValueError):
                pass

        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "flow",
                "background_style": "default",
                "columns": [
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
                                    "content": "批准",
                                },
                                "behaviors": [
                                    {
                                        "type": "callback",
                                        "value": value_allow,
                                    }
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
                                "element_id": f"d_{suf}",
                                "type": "danger_filled",
                                "size": "medium",
                                "width": "fill",
                                "text": {
                                    "tag": "plain_text",
                                    "content": "拒绝",
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
                        "tag": "markdown",
                        "content": (
                            "**给 Agent 更精确的指令**\n"
                            "在下方填写说明后点击「提交精确说明」。"
                            "**本次不会授予权限**；文本将回传给 Agent（未填写则仅视为未批准）。"
                        ),
                        "text_align": "left",
                        "text_size": "normal_v2",
                        "margin": "0px 0px 4px 0px",
                    },
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
                            "content": "例如：允许写入的具体目录、希望执行的完整命令……",
                        },
                        "label": {
                            "tag": "plain_text",
                            "content": "你的说明",
                        },
                        "label_position": "top",
                        "fallback": {
                            "tag": "fallback_text",
                            "text": {
                                "tag": "plain_text",
                                "content": "请升级至飞书 V6.8+ 以使用输入框",
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
                                            "content": "提交精确说明",
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
