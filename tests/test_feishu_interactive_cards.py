"""飞书工具 trace 与最终回复 Markdown 卡片结构。"""

from __future__ import annotations

import json

from frontend.feishu.interactive_cards import (
    build_agent_reply_markdown_card,
    build_tool_call_pending_card,
    build_tool_trace_card,
    format_arguments_for_tool_card,
)


def test_format_arguments_bash_plain() -> None:
    s = format_arguments_for_tool_card(
        "bash",
        {"command": "echo hi\nls", "timeout": 180},
    )
    assert "echo hi" in s
    assert "timeout: 180s" in s
    assert s.strip().startswith("{") is False


def test_build_tool_call_pending_card() -> None:
    card = build_tool_call_pending_card(
        tool_name="bash",
        arguments={"command": "ls"},
        tool_call_id="call_abc",
    )
    blob = json.dumps(card, ensure_ascii=False)
    assert "running" in blob
    assert "Input" in blob
    assert "ls" in blob


def test_build_tool_trace_card_roundtrip() -> None:
    card_no_in = build_tool_trace_card(
        tool_name="bash",
        success=True,
        message="ok",
        duration_ms=12,
        error=None,
    )
    s0 = json.dumps(card_no_in, ensure_ascii=False)
    assert "bash" in s0
    assert "Input" not in s0
    assert "Output" in s0

    card = build_tool_trace_card(
        tool_name="bash",
        success=True,
        message="ok",
        duration_ms=12,
        error=None,
        arguments={"command": "ls -la"},
        tool_call_id="call_xyz",
    )
    s = json.dumps(card, ensure_ascii=False)
    assert "Input" in s
    assert "ls -la" in s
    assert "Output" in s
    assert card["schema"] == "2.0"
    assert card["config"].get("width_mode") == "fill"
    assert "summary" in card["config"]


def test_build_tool_trace_card_includes_data_preview() -> None:
    card = build_tool_trace_card(
        tool_name="bash",
        success=False,
        message="命令执行结束，返回码为 127",
        duration_ms=6,
        error="NON_ZERO_EXIT",
        data_preview="--- stderr ---\npip: not found",
    )
    blob = json.dumps(card, ensure_ascii=False)
    assert "Streams" in blob
    assert "pip: not found" in blob


def test_build_agent_reply_markdown_card() -> None:
    card = build_agent_reply_markdown_card("## 标题\n- **粗体** [链接](https://example.com)")
    assert card["schema"] == "2.0"
    body = card["body"]["elements"][0]
    assert body["tag"] == "markdown"
    assert "标题" in body["content"]
    assert card["header"]["title"]["content"] == "回复"
    assert card["header"]["subtitle"]["content"] == "macchiato"

    mid = build_agent_reply_markdown_card(
        "x", header_title="助手（进行中）"
    )
    assert mid["header"]["title"]["content"] == "助手（进行中）"
    assert mid["header"]["template"] == "grey"
