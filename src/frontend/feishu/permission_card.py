"""飞书交互卡片：权限申请（批准 / 拒绝按钮，回传 card.action.trigger）。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 与回调解析一致（card_callback.py）
VALUE_KEY = "macchiato_permission"
ALLOW = "allow"
DENY = "deny"


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


def build_permission_request_card(
    *,
    permission_id: str,
    summary: str,
    kind: str = "",
    timeout_seconds: float | None = None,
    path_prefix: Optional[str] = None,
) -> Dict[str, Any]:
    """
    构建卡片 JSON 2.0（可直接作为 im/v1/messages interactive 的 content 对象）。

    参考：发送消息 — 卡片 interactive（JSON 字符串）
    https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/im-v1/message/create_json
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
    md_content = "\n".join(md_lines)

    value_allow: Dict[str, Any] = {VALUE_KEY: ALLOW, "permission_id": permission_id}
    value_deny: Dict[str, Any] = {VALUE_KEY: DENY, "permission_id": permission_id}
    if path_prefix:
        value_allow["path_prefix"] = path_prefix
        value_deny["path_prefix"] = path_prefix

    summary_preview = _truncate(f"权限 · {summary_t}", 100)

    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "summary": {"content": summary_preview},
        },
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "权限申请"},
            "subtitle": {
                "tag": "plain_text",
                "content": "Macchiato · 安全确认",
            },
            "padding": "16px 16px 16px 16px",
        },
        "body": {
            "direction": "vertical",
            "padding": "8px 16px 16px 16px",
            "vertical_spacing": "medium",
            "elements": [
                {
                    "tag": "markdown",
                    "content": md_content,
                    "text_align": "left",
                    "text_size": "normal_v2",
                    "margin": "0px 0px 8px 0px",
                },
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
                },
            ],
        },
    }


def interactive_content_string(card: Dict[str, Any]) -> str:
    """与飞书「方式三：使用卡片 JSON 发送」一致，整卡序列化为字符串。"""
    return json.dumps(card, ensure_ascii=False)


def parse_card_action_value(raw: Any) -> Tuple[str, str, Optional[str]]:
    """
    从 card.action.trigger 的 action.value 解析出 (permission_id, decision, path_prefix)。

    decision 为 allow | deny；path_prefix 可为空（批准时由 request_permission 推断并随卡片回传）。
    """
    data: Dict[str, Any]
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return "", "", None
    else:
        return "", "", None

    pid = str(data.get("permission_id") or "").strip()
    dec = str(data.get(VALUE_KEY) or data.get("decision") or "").strip().lower()
    if dec in ("approve", "yes", "ok", "y"):
        dec = ALLOW
    if dec in ("reject", "no", "n"):
        dec = DENY
    pfx = str(data.get("path_prefix") or "").strip() or None
    return pid, dec, pfx
