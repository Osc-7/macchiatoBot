"""Tests for shared LLM message normalization and summary preparation."""

from __future__ import annotations

import json

from agent_core.context.message_sanitize import (
    normalize_tool_sequence,
    prepare_messages_for_summary,
)


def _call(call_id: str, name: str = "read_file") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def test_normalize_synthesizes_missing_results_in_call_order() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_call("a"), _call("b")],
        },
        {"role": "tool", "tool_call_id": "b", "content": "b-result"},
        {"role": "user", "content": "next"},
    ]

    clean, stats = normalize_tool_sequence(messages)

    assert [m["role"] for m in clean] == ["assistant", "tool", "tool", "user"]
    assert [m.get("tool_call_id") for m in clean[1:3]] == ["a", "b"]
    assert "无有效输出" in clean[1]["content"]
    assert clean[2]["content"] == "b-result"
    assert stats["synthesized_tool_results"] == 1


def test_normalize_drops_orphan_mismatched_and_duplicate_results() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "leading-orphan", "content": "x"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_call("a")],
        },
        {"role": "tool", "tool_call_id": "a", "content": "first"},
        {"role": "tool", "tool_call_id": "wrong-id", "content": "wrong"},
        {"role": "tool", "tool_call_id": "a", "content": "duplicate"},
        {"role": "user", "content": "done"},
    ]

    clean, stats = normalize_tool_sequence(messages)

    assert [m["role"] for m in clean] == ["user", "assistant", "tool", "user"]
    assert clean[2]["content"] == "first"
    assert stats["dropped_orphan_tool_messages"] == 3
    assert stats["synthesized_tool_results"] == 0


def test_normalize_does_not_mutate_input() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_call("a")],
        },
        {"role": "tool", "tool_call_id": "orphan", "content": "x"},
    ]
    before = json.dumps(messages, ensure_ascii=False, sort_keys=True)

    normalize_tool_sequence(messages)

    assert json.dumps(messages, ensure_ascii=False, sort_keys=True) == before


def test_prepare_messages_for_summary_flattens_media_and_internal_fields() -> None:
    messages = [
        {
            "role": "user",
            "_turn_id": 7,
            "content": [
                {"type": "text", "text": "看这个"},
                {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
                {"type": "media_ref", "name": "clip.mp4"},
                {"type": "file", "filename": "notes.pdf"},
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "不应进入摘要请求",
            "tool_calls": [
                {
                    **_call("a"),
                    "extra_content": {"google": {"thought_signature": "sig"}},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "工具结果"},
    ]

    clean, stats = prepare_messages_for_summary(messages)

    assert "_turn_id" not in clean[0]
    assert "看这个" in clean[0]["content"]
    assert "[图片]" in clean[0]["content"]
    assert "[媒体:clip.mp4]" in clean[0]["content"]
    assert "[附件:notes.pdf]" in clean[0]["content"]
    assert "media_ref" not in json.dumps(clean, ensure_ascii=False)
    assert "reasoning_content" not in clean[1]
    assert "extra_content" not in clean[1]["tool_calls"][0]
    assert stats["flattened_content_blocks"] == 4


def test_prepare_messages_for_summary_drops_leading_orphan_tool() -> None:
    clean, stats = prepare_messages_for_summary(
        [
            {"role": "tool", "tool_call_id": "orphan", "content": "x"},
            {"role": "user", "content": "hi"},
        ]
    )

    assert [m["role"] for m in clean] == ["user"]
    assert stats["dropped_orphan_tool_messages"] == 1
