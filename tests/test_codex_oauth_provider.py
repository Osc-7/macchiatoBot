from __future__ import annotations

import json
from typing import Iterable

import pytest

from agent_core.llm.providers.codex_oauth_provider import _convert_messages, _parse_sse_stream


class _FakeSSE:
    status_code = 200

    def __init__(self, chunks: Iterable[str]) -> None:
        self._chunks = [chunk.encode("utf-8") for chunk in chunks]

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@pytest.mark.asyncio
async def test_parse_sse_stream_reconstructs_codex_tool_call_with_split_chunks():
    stream = "".join(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "id": "fc_1",
                        "type": "function_call",
                        "call_id": "call_abc",
                        "name": "bash",
                        "arguments": "",
                    },
                }
            ),
            _sse(
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "item_id": "fc_1",
                    "delta": '{"command"',
                }
            ),
            _sse(
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "item_id": "fc_1",
                    "delta": ':"pwd"}',
                }
            ),
            _sse(
                {
                    "type": "response.function_call_arguments.done",
                    "output_index": 0,
                    "item_id": "fc_1",
                    "arguments": '{"command":"pwd"}',
                }
            ),
            _sse(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": "fc_1",
                        "type": "function_call",
                        "call_id": "call_abc",
                        "name": "bash",
                        "arguments": '{"command":"pwd"}',
                    },
                }
            ),
            _sse(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_1",
                        "usage": {
                            "input_tokens": 11,
                            "output_tokens": 7,
                            "total_tokens": 18,
                        },
                    },
                }
            ),
        ]
    )

    response, response_id = await _parse_sse_stream(
        _FakeSSE([stream[:23], stream[23:101], stream[101:]])
    )

    assert response_id == "resp_1"
    assert response.content is None
    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "call_abc"
    assert response.tool_calls[0].name == "bash"
    assert response.tool_calls[0].arguments == {"command": "pwd"}
    assert response.usage is not None
    assert response.usage.total_tokens == 18


@pytest.mark.asyncio
async def test_parse_sse_stream_uses_completed_output_as_tool_call_fallback():
    response, _ = await _parse_sse_stream(
        _FakeSSE(
            [
                _sse(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_2",
                            "output": [
                                {
                                    "id": "fc_2",
                                    "type": "function_call",
                                    "call_id": "call_final",
                                    "name": "search_tools",
                                    "arguments": '{"query":"bash"}',
                                }
                            ],
                        },
                    }
                )
            ]
        )
    )

    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "call_final"
    assert response.tool_calls[0].name == "search_tools"
    assert response.tool_calls[0].arguments == {"query": "bash"}


@pytest.mark.asyncio
async def test_parse_sse_stream_uses_completed_text_without_duplicate_fallbacks():
    stream = "".join(
        [
            _sse({"type": "response.output_text.done", "text": "hello"}),
            _sse(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello"}],
                    },
                }
            ),
            _sse(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_3",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "hello"}],
                            }
                        ],
                    },
                }
            ),
        ]
    )

    response, _ = await _parse_sse_stream(_FakeSSE([stream]))

    assert response.content == "hello"
    assert response.tool_calls == []


@pytest.mark.asyncio
async def test_parse_sse_stream_extracts_reasoning_summary_and_encrypted_content():
    response, _ = await _parse_sse_stream(
        _FakeSSE(
            [
                _sse(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "id": "rs_1",
                            "type": "reasoning",
                            "encrypted_content": "enc_abc",
                            "summary": [
                                {
                                    "type": "summary_text",
                                    "text": "Reasoning summary line.",
                                }
                            ],
                        },
                    }
                ),
                _sse(
                    {
                        "type": "response.completed",
                        "response": {"id": "resp_reason"},
                    }
                ),
            ]
        )
    )

    assert response.reasoning_content == "Reasoning summary line."
    assert response.responses_reasoning_items is not None
    assert len(response.responses_reasoning_items) == 1
    assert response.responses_reasoning_items[0]["encrypted_content"] == "enc_abc"


def test_convert_messages_replays_saved_responses_reasoning_items():
    input_items, _ = _convert_messages(
        [
            {
                "role": "assistant",
                "content": "ok",
                "responses_reasoning_items": [
                    {
                        "type": "reasoning",
                        "encrypted_content": "enc_prev",
                        "summary": [{"type": "summary_text", "text": "s"}],
                    }
                ],
            },
            {"role": "user", "content": "next"},
        ]
    )
    assert isinstance(input_items, list) and len(input_items) >= 1
    first = input_items[0]
    assert first.get("type") == "reasoning"
    assert first.get("encrypted_content") == "enc_prev"
