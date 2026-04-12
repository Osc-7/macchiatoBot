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
_MAX_DATA_PREVIEW_IN_CARD = 6000
_MAX_REPLY_MARKDOWN = 12000
_COLLAPSE_ARGS_AT = 420
_COLLAPSE_RESULT_AT = 2800

# 视觉取向：中性灰主色 + 标签点状态（贴近 Claude Code / IDE 面板，非高饱和「运营横幅」）
_TOOL_HEADER_TEMPLATE = "grey"
_REPLY_HEADER_TEMPLATE = "grey"
# 仅使用文档示例中出现过的 standard_icon token，避免无效图标导致发送失败
_ICON_ASSISTANT = "robot_outlined"
_ICON_PANEL = "down-small-ccm_outlined"

_SUBTITLE_TOOL = "Tool run · macchiato"
_SUBTITLE_COMPOSER = "macchiato"

# 飞书卡片 markdown 的 text_size：仅影响字号档位，**无法指定字体族**（由客户端渲染）。
# 可选如 normal_v2、notation_v1（若客户端不支持会回退，可改回 normal_v2）
_MD_TOOL = "notation_v1"
_MD_REPLY = "normal_v2"


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


def format_arguments_for_tool_card(tool_name: str, arguments: Any) -> str:
    """
    工具调用展示用参数文本：bash 等优先「像终端一样」展示，避免整段 JSON。

    其它工具仍用 JSON/字符串化，保证信息完整。
    """
    name = (tool_name or "").strip().lower()
    if name == "bash" and isinstance(arguments, dict):
        cmd = arguments.get("command")
        if cmd is not None:
            lines: List[str] = [str(cmd).rstrip()]
            to = arguments.get("timeout")
            if to is not None and str(to).strip():
                lines.append("")
                lines.append(f"(timeout: {to}s)")
            return _truncate(_fence_body("\n".join(lines)), _MAX_TOOL_ARG_JSON)
    return _arguments_block(arguments)


def _card_config_base(*, summary: str) -> Dict[str, Any]:
    """共享：撑满会话宽度 + 会话列表摘要（见官方 config.summary）。"""
    return {
        "update_multi": True,
        "width_mode": "fill",
        "summary": {"content": _truncate(summary.strip(), 100)},
    }


def _markdown_el(
    content: str,
    *,
    margin: str = "0px 0px 0px 0px",
    text_size: str = "normal_v2",
) -> Dict[str, Any]:
    return {
        "tag": "markdown",
        "content": content,
        "text_align": "left",
        "text_size": text_size,
        "margin": margin,
    }


def _hr_el(seed: str) -> Dict[str, Any]:
    return {"tag": "hr", "element_id": _element_id("hr", seed), "margin": "4px 0px 12px 0px"}


def _tool_input_body_elements(
    tool_name: str,
    arguments: Any,
    *,
    seed: str,
) -> List[Dict[str, Any]]:
    """与 build_tool_call_pending_card 中 Input 区块一致，供结果卡 PATCH 时复用。"""
    name = (tool_name or "unknown").strip() or "unknown"
    args_text = format_arguments_for_tool_card(name, arguments)
    args_len = len(args_text)
    input_md = "\n".join(
        [
            "#### Input",
            "",
            "```text",
            args_text,
            "```",
        ]
    )
    elements: List[Dict[str, Any]] = []
    if args_len > _COLLAPSE_ARGS_AT:
        elements.append(
            {
                "tag": "collapsible_panel",
                "element_id": _element_id("cp_in", seed),
                "expanded": True,
                "vertical_spacing": "8px",
                "padding": "4px 4px 8px 4px",
                "header": {
                    "title": {
                        "tag": "markdown",
                        "content": "**Input** · expand",
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
                "background_color": "grey",
                "border": {"color": "grey", "corner_radius": "6px"},
                "elements": [
                    _markdown_el(input_md, margin="0px", text_size=_MD_TOOL),
                ],
            }
        )
    else:
        elements.append(
            _markdown_el(input_md, margin="0px", text_size=_MD_TOOL),
        )
    return elements


def build_tool_call_pending_card(
    *,
    tool_name: str,
    arguments: Any,
    tool_call_id: str,
) -> Dict[str, Any]:
    """
    tool_call 阶段立即发送：只含 Input，标签 running。
    与 tool_result 卡片分离，避免长时间执行时聊天区空白。
    """
    name = (tool_name or "unknown").strip() or "unknown"
    name_short = _truncate(name, 36)
    seed = f"{tool_call_id}|{name_short}"
    elements = _tool_input_body_elements(name, arguments, seed=seed)

    summary = _truncate(f"{name_short} · running", 100)
    header: Dict[str, Any] = {
        "template": _TOOL_HEADER_TEMPLATE,
        "title": {
            "tag": "plain_text",
            "content": f"› {name_short} · …",
        },
        "subtitle": {"tag": "plain_text", "content": _SUBTITLE_TOOL},
        "text_tag_list": [
            {
                "tag": "text_tag",
                "element_id": _element_id("tg_run", seed),
                "text": {"tag": "plain_text", "content": "running"},
                "color": "blue",
            },
        ],
        "padding": "12px 14px 12px 14px",
    }
    return {
        "schema": "2.0",
        "config": _card_config_base(summary=summary),
        "header": header,
        "body": {
            "direction": "vertical",
            "padding": "4px 14px 14px 14px",
            "vertical_spacing": "medium",
            "horizontal_spacing": "medium",
            "elements": elements,
        },
    }


def build_tool_trace_card(
    *,
    tool_name: str,
    success: bool,
    message: str,
    duration_ms: int,
    error: Optional[str],
    data_preview: Optional[str] = None,
    arguments: Optional[Any] = None,
    tool_call_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    tool_result 阶段发送：Output / streams / error；若提供 ``arguments``（与 tool_call 一致），
    同时保留 **Input** 区块，便于 PATCH 后仍能看见命令与参数。
    """
    name = (tool_name or "unknown").strip() or "unknown"
    name_short = _truncate(name, 36)
    status = "ok" if success else "error"
    tpl = _TOOL_HEADER_TEMPLATE
    tag_status_color = "green" if success else "red"
    msg = _truncate(_fence_body(message or ""), _MAX_TOOL_MESSAGE)
    err_line = ""
    if error and str(error).strip():
        err_line = f"\n\n**exit / error**\n\n`{_truncate(str(error), 280)}`"
    dur = int(duration_ms) if duration_ms is not None else 0
    tc_seed = (tool_call_id or "").strip() or "no_id"
    seed = f"{tc_seed}|{name}|{dur}"

    dp = (data_preview or "").strip()
    if dp:
        dp = _truncate(_fence_body(dp), _MAX_DATA_PREVIEW_IN_CARD)
    result_body = (msg if msg else "（无文本消息）") + err_line
    if dp:
        result_body += (
            "\n\n#### Streams\n\n"
            "```text\n"
            f"{dp}\n"
            "```"
        )
    result_md = "\n".join(["#### Output", "", result_body])

    elements: List[Dict[str, Any]] = []
    if arguments is not None:
        in_seed = f"{tc_seed}|{name_short}|in"
        elements.extend(_tool_input_body_elements(name, arguments, seed=in_seed))
        elements.append(_hr_el(f"{seed}|hr"))

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
                        "content": "**Output** · long",
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
                "background_color": "grey",
                "border": {"color": "grey", "corner_radius": "6px"},
                "elements": [
                    _markdown_el(result_md, margin="0px", text_size=_MD_TOOL),
                ],
            }
        )
    else:
        elements.append(_markdown_el(result_md, margin="0px", text_size=_MD_TOOL))

    summary = f"{name_short} · {status} · {dur}ms"

    header = {
        "template": tpl,
        "title": {
            "tag": "plain_text",
            "content": f"› {name_short}",
        },
        "subtitle": {
            "tag": "plain_text",
            "content": _SUBTITLE_TOOL,
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
        "padding": "12px 14px 12px 14px",
    }

    return {
        "schema": "2.0",
        "config": _card_config_base(summary=summary),
        "header": header,
        "body": {
            "direction": "vertical",
            "padding": "4px 14px 14px 14px",
            "vertical_spacing": "medium",
            "horizontal_spacing": "medium",
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

    tpl = _REPLY_HEADER_TEMPLATE
    tag_label = "stream" if is_intermediate else "reply"

    header: Dict[str, Any] = {
        "template": tpl,
        "title": {"tag": "plain_text", "content": title},
        "subtitle": {
            "tag": "plain_text",
            "content": _SUBTITLE_COMPOSER,
        },
        "icon": {
            "tag": "standard_icon",
            "token": _ICON_ASSISTANT,
            "color": "grey",
        },
        "text_tag_list": [
            {
                "tag": "text_tag",
                "element_id": _element_id("tg_md", summary_seed),
                "text": {"tag": "plain_text", "content": tag_label},
                "color": "violet" if not is_intermediate else "blue",
            },
        ],
        "padding": "12px 14px 12px 14px",
    }

    return {
        "schema": "2.0",
        "config": _card_config_base(summary=summary),
        "header": header,
        "body": {
            "direction": "vertical",
            "padding": "6px 14px 16px 14px",
            "vertical_spacing": "medium",
            "elements": [
                _markdown_el(content, margin="0px 0px 0px 0px", text_size=_MD_REPLY),
            ],
        },
    }
