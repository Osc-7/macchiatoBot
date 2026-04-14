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
# 多题同卡：每题独立「其他」输入框 name 前缀
ASK_USER_CUSTOM_Q_PREFIX = "macchiato_custom__"


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
    if form_value and isinstance(form_value, dict) and mode == ASK_CUSTOM:
        # 合并卡：回调 value 可能不含 question_id，从首个非空的 macchiato_custom__* 解析
        for fk in sorted(form_value.keys()):
            fv = form_value.get(fk)
            if (
                isinstance(fk, str)
                and fk.startswith(ASK_USER_CUSTOM_Q_PREFIX)
                and fv is not None
                and str(fv).strip()
            ):
                merged["custom_text"] = str(fv).strip()
                merged["question_id"] = fk[len(ASK_USER_CUSTOM_Q_PREFIX) :]
                break
        if not (merged.get("custom_text") or "").strip() and ASK_USER_CUSTOM_FIELD in form_value:
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
    options: List[str],
    suf: str,
) -> List[Dict[str, Any]]:
    """将选项排成多行，每行最多 2 个按钮。

    注意：飞书对按钮 behaviors.value 序列化后长度有限制，禁止塞入长题干；
    仅保留 batch_id / question_id / selected_option。
    """
    columns: List[Dict[str, Any]] = []
    pair: List[Dict[str, Any]] = []
    for i, opt in enumerate(options):
        label = _truncate(opt, 40)
        value_pick: Dict[str, Any] = {
            ASK_USER_VALUE_KEY: ASK_PICK,
            "batch_id": batch_id,
            "question_id": question_id,
            "selected_option": opt,
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

    md_lines = [f"**{prompt_t}**{idx_hint}"]
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
                options=opts,
                suf=suf,
            )
        )
        value_custom: Dict[str, Any] = {
            ASK_USER_VALUE_KEY: ASK_CUSTOM,
            "batch_id": batch_id,
            "question_id": qid,
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
                        "max_length": 1000,
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
            "template": "wathet",
            "title": {"tag": "plain_text", "content": "请选择"},
            "subtitle": {
                "tag": "plain_text",
                "content": (
                    f"第 {question_index + 1} / {total_questions} 题"
                    if total_questions > 1
                    else ("待选择" if resolved is None else "已完成")
                ),
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


def _resolved_display_from_partial_row(row: Dict[str, Any]) -> Optional[str]:
    ct = str(row.get("custom_text") or "").strip()
    so = str(row.get("selected_option") or "").strip() if row.get("selected_option") else ""
    if ct:
        return _truncate(ct, 480)
    if so:
        return _truncate(so, 120)
    return None


def build_ask_user_card_from_registry_snapshot(
    snap: Dict[str, Any], question_id: str
) -> Dict[str, Any]:
    """由 :func:`take_ask_user_snapshot` 生成**单题**飞书卡片（多题 batch 下每题一条消息）。"""
    bid = str(snap.get("batch_id") or "")
    qs_raw = snap.get("questions") or []
    if not isinstance(qs_raw, list):
        qs_raw = []
    qs_list = [q for q in qs_raw if isinstance(q, dict)]
    total = len(qs_list)
    qid_target = str(question_id or "").strip()
    custom_label = str(
        snap.get("custom_option_label") or "其他（请填写具体说明）"
    )

    question: Optional[Dict[str, Any]] = None
    q_index = 0
    for i, q in enumerate(qs_list):
        qidi = str(q.get("id") or "").strip() or f"q{i + 1}"
        if qidi == qid_target:
            question = q
            q_index = i
            break
    if question is None:
        raise ValueError(f"snapshot 中无题目 question_id={question_id!r}")

    pr = snap.get("partial") or {}
    if not isinstance(pr, dict):
        pr = {}
    row = pr.get(qid_target)
    resolved: Optional[str] = None
    if isinstance(row, dict):
        resolved = _resolved_display_from_partial_row(row)

    return build_ask_user_question_card(
        batch_id=bid,
        question=question,
        custom_option_label=custom_label,
        question_index=q_index,
        total_questions=total,
        resolved=resolved,
    )
