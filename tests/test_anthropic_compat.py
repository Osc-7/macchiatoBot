"""AnthropicCompatProvider：响应解析（扩展思考 / 多 text 块兼容）。"""

from __future__ import annotations

import pytest

from agent_core.llm.capabilities import Capabilities
from agent_core.llm.providers import AnthropicCompatProvider


def _provider(*, caps: Capabilities) -> AnthropicCompatProvider:
    return AnthropicCompatProvider(
        name="t",
        base_url="https://example.com/v1",
        api_key="sk-x",
        model="kimi-for-coding",
        capabilities=caps,
        temperature=0.7,
        max_tokens=4096,
        request_timeout_seconds=30.0,
        stream=False,
        vendor_params={},
    )


def test_parse_extended_thinking_blocks_split_reasoning_and_content():
    p = _provider(
        caps=Capabilities(reasoning_content=True),
    )
    raw = {
        "stop_reason": "end_turn",
        "content": [
            {
                "type": "thinking",
                "thinking": "internal chain",
                "signature": "sig",
            },
            {"type": "text", "text": "Hello user."},
        ],
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
    r = p._parse_response(raw)
    assert r.reasoning_content == "internal chain"
    assert r.content == "Hello user."


def test_parse_multiple_text_blocks_last_is_reply_when_reasoning_cap():
    """部分兼容端点把「分析」放在前几个 text 块、最后一块才是对用户回复。"""
    p = _provider(caps=Capabilities(reasoning_content=True))
    raw = {
        "stop_reason": "end_turn",
        "content": [
            {"type": "text", "text": "用户意思是先确认配置。"},
            {"type": "text", "text": "好的，已切换成功。"},
        ],
        "usage": {},
    }
    r = p._parse_response(raw)
    assert "用户意思是" in (r.reasoning_content or "")
    assert r.content == "好的，已切换成功。"


def test_parse_multiple_text_joined_when_no_reasoning_cap():
    p = _provider(caps=Capabilities(reasoning_content=False))
    raw = {
        "stop_reason": "end_turn",
        "content": [
            {"type": "text", "text": "Part one."},
            {"type": "text", "text": "Part two."},
        ],
        "usage": {},
    }
    r = p._parse_response(raw)
    assert r.reasoning_content is None
    assert r.content == "Part one.\nPart two."


def test_convert_prefers_anthropic_message_content_for_assistant():
    """有 anthropic_message_content 时应用 API 原样块（含 thinking），并按块内 tool_use id 收集 tool_result。"""
    p = _provider(caps=Capabilities())
    amc = [
        {"type": "thinking", "thinking": "x", "signature": "sig"},
        {"type": "text", "text": "查一下"},
        {"type": "tool_use", "id": "tu_a", "name": "call_tool", "input": {"name": "get_events"}},
        {"type": "tool_use", "id": "tu_b", "name": "call_tool", "input": {"name": "get_tasks"}},
    ]
    _, msgs = p._convert_messages(
        [
            {
                "role": "assistant",
                "content": "查一下",
                "tool_calls": [],
                "anthropic_message_content": amc,
            },
            {"role": "tool", "tool_call_id": "tu_a", "content": "{}"},
            {"role": "tool", "tool_call_id": "tu_b", "content": "{}"},
        ]
    )
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["content"] == amc
    assert len(msgs[1]["content"]) == 2
    assert msgs[1]["content"][0]["tool_use_id"] == "tu_a"


def test_gather_tool_results_across_interleaved_user_message():
    """两条 role=tool 之间夹了普通 user 时，仍应合并为一条 user + 两个 tool_result。"""
    p = _provider(caps=Capabilities())
    _, msgs = p._convert_messages(
        [
            {
                "role": "assistant",
                "content": "并行查",
                "tool_calls": [
                    {
                        "id": "tool_a",
                        "type": "function",
                        "function": {"name": "get_events", "arguments": "{}"},
                    },
                    {
                        "id": "tool_b",
                        "type": "function",
                        "function": {"name": "get_tasks", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "tool_a", "content": "{}"},
            {"role": "user", "content": "中间插一句"},
            {"role": "tool", "tool_call_id": "tool_b", "content": "{}"},
        ]
    )
    assert len(msgs) == 3
    assert msgs[0]["role"] == "assistant"
    assert msgs[1]["role"] == "user"
    assert len(msgs[1]["content"]) == 2
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"] == "中间插一句"


def test_merge_two_consecutive_tool_results_into_one_user_message():
    """并行工具：两条 role=tool 应对应为一条 user，内含两个 tool_result 块。"""
    p = _provider(caps=Capabilities())
    _, msgs = p._convert_messages(
        [
            {
                "role": "assistant",
                "content": "调用中",
                "tool_calls": [
                    {
                        "id": "tool_a",
                        "type": "function",
                        "function": {
                            "name": "get_events",
                            "arguments": "{}",
                        },
                    },
                    {
                        "id": "tool_b",
                        "type": "function",
                        "function": {
                            "name": "get_tasks",
                            "arguments": "{}",
                        },
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "tool_a", "content": "{}"},
            {"role": "tool", "tool_call_id": "tool_b", "content": "{}"},
        ]
    )
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    assert len(user_msgs) == 1
    blocks = user_msgs[0]["content"]
    assert len(blocks) == 2
    assert blocks[0]["type"] == "tool_result"
    assert blocks[1]["type"] == "tool_result"
    assert blocks[0]["tool_use_id"] == "tool_a"
    assert blocks[1]["tool_use_id"] == "tool_b"


def test_convert_messages_openai_nested_tool_calls():
    """Agent 存的 tool_calls 为 OpenAI 嵌套格式，必须能转成合法 Anthropic tool_use。"""
    p = _provider(caps=Capabilities())
    system, msgs = p._convert_messages(
        [
            {
                "role": "assistant",
                "content": "先搜工具",
                "tool_calls": [
                    {
                        "id": "tool_abc",
                        "type": "function",
                        "function": {
                            "name": "search_tools",
                            "arguments": '{"query": "x", "tags": ["a"]}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tool_abc", "content": "{}"},
        ]
    )
    assert system is None
    assert len(msgs) == 2
    parts = msgs[0]["content"]
    assert isinstance(parts, list)
    tu = [x for x in parts if x.get("type") == "tool_use"][0]
    assert tu["name"] == "search_tools"
    assert tu["input"] == {"query": "x", "tags": ["a"]}


@pytest.mark.asyncio
async def test_close():
    p = _provider(caps=Capabilities())
    await p.close()
