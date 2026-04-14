"""飞书交互卡片：ask_user（选择题 + 自由填写）。"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

ASK_USER_VALUE_KEY = "macchiato_ask_user"
ASK_PICK = "pick"
ASK_CUSTOM = "custom"
# 表单内输入框 name（单卡内唯一）
ASK_USER_CUSTOM_FIELD = "macchiato_ask_custom"


def _truncate(s: str, max_len: int) -> str:
    t = (s or "").strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _element_suffix(batch_id: str, question_id: str) -> str:
    raw = f"{batch_id}:{question_id}"
    h = re.sub(r"[^0-9a-fA-F]", "", raw)[:8]
    if len(h) < 8:
        h = (h + "00000000")[:8]
    return h.lower()


def merge_ask_user_action_value(
    raw_val: Any,
    form_value: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """合并按钮 value 与表单（仅「提交说明」回调合并输入框，避免点选项时带入表单残留）。"""
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
    mode = str(merged.get(ASK_USER_VALUE_KEY) or "").strip().lower()
    if (
        form_value
        and isinstance(form_value, dict)
        and ASK_USER_CUSTOM_FIELD in form_value
        and mode == ASK_CUSTOM
    ):
        merged["custom_text"] = form_value[ASK_USER_CUSTOM_FIELD]
    return merged


def parse_ask_user_card_callback(
    raw_val: Any,
    form_value: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, str, str, Optional[str], Optional[str]]:
    """
    解析 ask_user 卡片交互。

    返回 (batch_id, mode, question_id, selected_option, custom_text, prompt_echo)
    mode 为 pick | custom
    """
    data = merge_ask_user_action_value(raw_val, form_value)
    bid = str(data.get("batch_id") or data.get("ask_user_id") or "").strip()
    mode = str(data.get(ASK_USER_VALUE_KEY) or "").strip().lower()
    qid = str(data.get("question_id") or "").strip()
    so = data.get("selected_option")
    selected = str(so).strip() if so is not None and str(so).strip() else None
    ct_raw = data.get("custom_text")
    custom_text = str(ct_raw).strip() if ct_raw is not None and str(ct_raw).strip() else None
    pe = str(data.get("prompt_echo") or "").strip()
    return bid, mode, qid, selected or "", custom_text, pe


def _option_button_columns(
    *,
    batch_id: str,
    question_id: str,
    prompt_echo: str,
    options: List[str],
    suf: str,
) -> List[Dict[str, Any]]:
    """将选项排成多行，每行最多 2 个按钮。"""
    columns: List[Dict[str, Any]] = []
    pair: List[Dict[str, Any]] = []
    for i, opt in enumerate(options):
        label = _truncate(opt, 40)
        value_pick: Dict[str, Any] = {
            ASK_USER_VALUE_KEY: ASK_PICK,
            "batch_id": batch_id,
            "question_id": question_id,
            "selected_option": opt,
            "prompt_echo": prompt_echo,
        }
        btn = {
            "tag": "button",
            "element_id": f"au_{suf}_{i}",
            "type": "primary_filled",
            "size": "medium",
            "width": "fill",
            "text": {"tag": "plain_text", "content": label},
            "behaviors": [{"type": "callback", "value": value_pick}],
        }
        pair.append(
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [btn],
            }
        )
        if len(pair) == 2:
            columns.append(
                {
                    "tag": "column_set",
                    "flex_mode": "flow",
                    "background_style": "default",
                    "columns": pair,
                }
            )
            pair = []
    if pair:
        columns.append(
            {
                "tag": "column_set",
                "flex_mode": "flow",
                "background_style": "default",
                "columns": pair,
            }
        )
    return columns


def build_ask_user_question_card(
    *,
    batch_id: str,
    question: Dict[str, Any],
    custom_option_label: str,
    question_index: int,
    total_questions: int,
    resolved: Optional[str] = None,
) -> Dict[str, Any]:
    """单题卡片：选项按钮 + 底部「其他」表单（与 Cursor 风格一致）。"""
    qid = str(question.get("id") or "").strip() or f"q{question_index + 1}"
    prompt = str(question.get("prompt") or "").strip()
    options = question.get("options") or []
    if not isinstance(options, list):
        options = []
    opts = [str(o).strip() for o in options if str(o).strip()]

    suf = _element_suffix(batch_id, qid)
    prompt_t = _truncate(prompt, 900)
    idx_hint = f"（{question_index + 1}/{total_questions}）" if total_questions > 1 else ""

    md_lines = [
        f"**{prompt_t}**{idx_hint}",
        f"`batch_id`: `{batch_id}`",
        f"`question_id`: `{qid}`",
    ]
    if resolved:
        md_lines.append(f"✅ **{resolved}**")

    md_content = "\n".join(md_lines)
    summary_preview = _truncate(prompt_t, 80)

    elements: List[Dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": md_content,
            "text_align": "left",
            "text_size": "normal_v2",
            "margin": "0px 0px 8px 0px",
        }
    ]

    if resolved is None:
        elements.extend(
            _option_button_columns(
                batch_id=batch_id,
                question_id=qid,
                prompt_echo=prompt_t,
                options=opts,
                suf=suf,
            )
        )
        value_custom: Dict[str, Any] = {
            ASK_USER_VALUE_KEY: ASK_CUSTOM,
            "batch_id": batch_id,
            "question_id": qid,
            "prompt_echo": prompt_t,
        }
        elements.append(
            {
                "tag": "form",
                "name": f"form_ask_user_{suf}",
                "element_id": f"fm_au_{suf}",
                "direction": "vertical",
                "vertical_spacing": "medium",
                "elements": [
                    {
                        "tag": "input",
                        "name": ASK_USER_CUSTOM_FIELD,
                        "input_type": "multiline_text",
                        "rows": 3,
                        "max_length": 2000,
                        "width": "fill",
                        "required": False,
                        "placeholder": {
                            "tag": "plain_text",
                            "content": custom_option_label or "其他（请填写具体说明）",
                        },
                        "label": {
                            "tag": "plain_text",
                            "content": custom_option_label or "其他（请填写具体说明）",
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
                                        "name": f"btn_custom_{suf}",
                                        "type": "primary",
                                        "size": "medium",
                                        "text": {
                                            "tag": "plain_text",
                                            "content": "提交说明",
                                        },
                                        "form_action_type": "submit",
                                        "behaviors": [
                                            {
                                                "type": "callback",
                                                "value": value_custom,
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
            "template": "blue",
            "title": {"tag": "plain_text", "content": "Ask user"},
            "subtitle": {
                "tag": "plain_text",
                "content": "Pending" if resolved is None else "Done",
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
