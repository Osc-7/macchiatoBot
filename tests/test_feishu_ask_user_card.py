"""飞书 ask_user 卡片 value / 表单合并。"""

from __future__ import annotations

from frontend.feishu.ask_user_card import (
    ASK_CUSTOM,
    ASK_PICK,
    ASK_USER_CUSTOM_FIELD,
    ASK_USER_CUSTOM_Q_PREFIX,
    ASK_USER_VALUE_KEY,
    merge_ask_user_action_value,
)


def test_merge_ask_user_pick_ignores_form_residual() -> None:
    raw = {
        ASK_USER_VALUE_KEY: ASK_PICK,
        "batch_id": "b1",
        "question_id": "q1",
        "selected_option": "选项A",
    }
    merged = merge_ask_user_action_value(
        raw,
        {ASK_USER_CUSTOM_FIELD: "残留文字不应影响点选"},
    )
    assert merged.get("custom_text") is None
    assert merged.get("selected_option") == "选项A"


def test_merge_ask_user_custom_prefixed_field() -> None:
    raw = {
        ASK_USER_VALUE_KEY: ASK_CUSTOM,
        "batch_id": "b1",
        "question_id": "q_sleep",
    }
    merged = merge_ask_user_action_value(
        raw,
        {f"{ASK_USER_CUSTOM_Q_PREFIX}q_sleep": "自定义内容"},
    )
    assert merged.get("custom_text") == "自定义内容"
    assert merged.get("question_id") == "q_sleep"


def test_merge_ask_user_combined_value_without_question_id() -> None:
    """合并卡提交说明：behaviors.value 仅含 batch_id + mode，question_id 从表单字段名解析。"""
    raw = {
        ASK_USER_VALUE_KEY: ASK_CUSTOM,
        "batch_id": "b1",
    }
    merged = merge_ask_user_action_value(
        raw,
        {f"{ASK_USER_CUSTOM_Q_PREFIX}food_preference": "寿司"},
    )
    assert merged.get("custom_text") == "寿司"
    assert merged.get("question_id") == "food_preference"


def test_merge_ask_user_custom_takes_form() -> None:
    raw = {
        ASK_USER_VALUE_KEY: ASK_CUSTOM,
        "batch_id": "b1",
        "question_id": "q1",
    }
    merged = merge_ask_user_action_value(
        raw,
        {ASK_USER_CUSTOM_FIELD: "  我的说明  "},
    )
    assert merged.get("custom_text") == "  我的说明  "
