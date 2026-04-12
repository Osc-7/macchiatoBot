"""飞书交互卡片：工具 trace、最终回复（Markdown 渲染）。

视觉与结构参考飞书卡片 JSON 2.0：
https://open.feishu.cn/document/feishu-cards/feishu-card-overview
https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-structure
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

# 飞书卡片内 markdown 单元素不宜过长，避免发送失败
_MAX_TOOL_ARG_JSON = 2400
_MAX_TOOL_MESSAGE = 4000
_MAX_REPLY_MARKDOWN = 12000
_COLLAPSE_ARGS_AT = 420
_COLLAPSE_RESULT_AT = 2800

# 仅使用文档示例中出现过的 standard_icon token，避免无效图标导致发送失败
_ICON_ASSISTANT = "robot_outlined"
_ICON_PANEL = "down-small-ccm_outlined"


def _truncate(s: str, max_len: int) -> str:
    t = (s or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _element_id(kind: str, seed: str) -> str:
    """同一张卡片内唯一的 element_id：字母开头，≤20 字符。"""
    h = hashlib.md5(seed.encode("utf-8")).hexdigest()[:8]
    raw = f"{kind}_{h}"
    if not raw[0].isalpha():
        raw = f"id_{raw}"
    return raw[:20]


def _fence_body(s: str) -> str:
    """避免与 markdown 围栏冲突。"""
    t = s.replace("```", "`\u200b``")
    return t


def _arguments_block(arguments: Any) -> str:
    if arguments is None:
        return "（无参数）"
    if isinstance(arguments, str):
        raw = arguments.strip()
        if not raw:
            return "（无参数）"
        return _truncate(_fence_body(raw), _MAX_TOOL_ARG_JSON)
    try:
        dumped = json.dumps(arguments, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        dumped = str(arguments)
    return _truncate(_fence_body(dumped), _MAX_TOOL_ARG_JSON)


def _card_config_base(*, summary: str) -> Dict[str, Any]:
    """共享：撑满会话宽度 + 会话列表摘要（见官方 config.summary）。"""
    return {
        "update_multi": True,
        "width_mode": "fill",
        "summary": {"content": _truncate(summary.strip(), 100)},
    }


def _markdown_el(content: str, *, margin: str = "0px 0px 0px 0px") -> Dict[str, Any]:
    return {
        "tag": "markdown",
        "content": content,
        "text_align": "left",
        "text_size": "normal_v2",
        "margin": margin,
    }


def _hr_el(seed: str) -> Dict[str, Any]:
    return {"tag": "hr", "element_id": _element_id("hr", seed), "margin": "4px 0px 12px 0px"}


def build_tool_trace_card(
    *,
    tool_name: str,
    arguments: Any,
    success: bool,
    message: str,
    duration_ms: int,
    error: Optional[str],
) -> Dict[str, Any]:
    """
    单次工具调用完成卡片：标题区标签 + 分割线 + 参数区（可折叠）+ 输出区（过长可折叠）。

    在 tool_result trace 后发送；schema 2.0。
    """
    name = (tool_name or "unknown").strip() or "unknown"
    name_short = _truncate(name, 36)
    status = "成功" if success else "失败"
    tpl = "turquoise" if success else "red"
    tag_status_color = "turquoise" if success else "red"
    msg = _truncate(_fence_body(message or ""), _MAX_TOOL_MESSAGE)
    err_line = ""
    if error and str(error).strip():
        err_line = f"\n\n**错误码**\n\n`{_truncate(str(error), 280)}`"
    dur = int(duration_ms) if duration_ms is not None else 0

    args_text = _arguments_block(arguments)
    args_len = len(args_text)
    seed = f"{name}|{dur}|{args_len}"

    input_md = "\n".join(
        [
            "#### 调用参数",
            "",
            "```text",
            args_text,
            "```",
        ]
    )

    result_md = "\n".join(
        [
            "#### 返回内容",
            "",
            (msg if msg else "（无文本消息）") + err_line,
        ]
    )

    elements: List[Dict[str, Any]] = []

    if args_len > _COLLAPSE_ARGS_AT:
        elements.append(
            {
                "tag": "collapsible_panel",
                "element_id": _element_id("cp_in", seed),
                "expanded": False,
                "vertical_spacing": "8px",
                "padding": "4px 4px 8px 4px",
                "header": {
                    "title": {
                        "tag": "markdown",
                        "content": "**调用参数**（点击展开）",
                    },
                    "vertical_align": "center",
                    "icon": {
                        "tag": "standard_icon",
                        "token": _ICON_PANEL,
                        "color": "grey",
                        "size": "14px 14px",
                    },
                    "icon_position": "right",
                    "icon_expanded_angle": -180,
                },
                "border": {"color": "grey", "corner_radius": "8px"},
                "elements": [_markdown_el(input_md, margin="0px")],
            }
        )
    else:
        elements.append(_markdown_el(input_md, margin="0px 0px 4px 0px"))

    elements.append(_hr_el(f"mid_{seed}"))

    if len(result_md) > _COLLAPSE_RESULT_AT:
        elements.append(
            {
                "tag": "collapsible_panel",
                "element_id": _element_id("cp_out", seed),
                "expanded": True,
                "vertical_spacing": "8px",
                "padding": "4px 4px 8px 4px",
                "header": {
                    "title": {
                        "tag": "markdown",
                        "content": "**返回内容**（较长，可折叠）",
                    },
                    "vertical_align": "center",
                    "icon": {
                        "tag": "standard_icon",
                        "token": _ICON_PANEL,
                        "color": "grey",
                        "size": "14px 14px",
                    },
                    "icon_position": "right",
                    "icon_expanded_angle": -180,
                },
                "border": {"color": "grey", "corner_radius": "8px"},
                "elements": [
                    _markdown_el(
                        (msg if msg else "（无文本消息）") + err_line,
                        margin="0px",
                    )
                ],
            }
        )
    else:
        elements.append(_markdown_el(result_md, margin="0px"))

    summary = f"{name_short} · {status} · {dur}ms"

    header: Dict[str, Any] = {
        "template": tpl,
        "title": {
            "tag": "plain_text",
            "content": f"工具 · {name_short}",
        },
        "subtitle": {
            "tag": "plain_text",
            "content": "Macchiato · tool trace",
        },
        "text_tag_list": [
            {
                "tag": "text_tag",
                "element_id": _element_id("tg_ok", seed),
                "text": {"tag": "plain_text", "content": status},
                "color": tag_status_color,
            },
            {
                "tag": "text_tag",
                "element_id": _element_id("tg_ms", seed),
                "text": {"tag": "plain_text", "content": f"{dur} ms"},
                "color": "neutral",
            },
        ],
        "padding": "16px 16px 16px 16px",
    }

    return {
        "schema": "2.0",
        "config": _card_config_base(summary=summary),
        "header": header,
        "body": {
            "direction": "vertical",
            "padding": "4px 16px 18px 16px",
            "vertical_spacing": "medium",
            "elements": elements,
        },
    }


def build_agent_reply_markdown_card(
    markdown: str,
    *,
    header_title: str = "回复",
) -> Dict[str, Any]:
    """
    助手输出：卡片内 Markdown；标题区副标题 + 图标 + 会话摘要。

    纯文本消息仍应走 send_text_message + filter_markdown_for_feishu。
    """
    raw = markdown or ""
    content = _truncate(raw, _MAX_REPLY_MARKDOWN)
    title = _truncate((header_title or "回复").strip() or "回复", 40)
    summary_seed = content[:80].replace("\n", " ")
    is_intermediate = "进行中" in title

    summary = _truncate(f"{title} · {summary_seed}", 100)

    tpl = "indigo" if not is_intermediate else "wathet"

    header: Dict[str, Any] = {
        "template": tpl,
        "title": {"tag": "plain_text", "content": title},
        "subtitle": {
            "tag": "plain_text",
            "content": "Macchiato Assistant",
        },
        "icon": {
            "tag": "standard_icon",
            "token": _ICON_ASSISTANT,
            "color": "white",
        },
        "text_tag_list": [
            {
                "tag": "text_tag",
                "element_id": _element_id("tg_md", summary_seed),
                "text": {"tag": "plain_text", "content": "Markdown"},
                "color": "neutral",
            },
        ],
        "padding": "16px 16px 16px 16px",
    }

    return {
        "schema": "2.0",
        "config": _card_config_base(summary=summary),
        "header": header,
        "body": {
            "direction": "vertical",
            "padding": "8px 16px 20px 16px",
            "vertical_spacing": "medium",
            "elements": [
                _markdown_el(content, margin="0px 0px 0px 0px"),
            ],
        },
    }
