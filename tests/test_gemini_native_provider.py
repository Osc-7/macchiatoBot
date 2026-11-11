"""Tests for GeminiNativeProvider (v1beta GenerateContent / streamGenerateContent)."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from agent_core.context.conversation import ConversationContext
from agent_core.context.message_sanitize import (
    normalize_tool_sequence,
    prepare_messages_for_summary,
)
from agent_core.llm.capabilities import Capabilities
from agent_core.llm.providers.gemini_native import (
    GeminiNativeProvider,
    _clean_json_schema_for_gemini,
    _coalesce_streamed_gemini_parts,
)


def _make_provider(*, stream: bool = True) -> GeminiNativeProvider:
    return GeminiNativeProvider(
        name="custom_gemini_4_carbon",
        base_url="https://54741234.xyz/antigravity",
        api_key="sk-test",
        model="gemini-4-carbon-high",
        capabilities=Capabilities(
            vision=True,
            function_calling=True,
            parallel_tool_calls=True,
            reasoning_content=True,
            context_window=1_000_000,
        ),
        temperature=0.7,
        max_tokens=8192,
        stream=stream,
        vendor_params={"trust_env": False},
    )


def test_api_base_normalization() -> None:
    p1 = _make_provider()
    assert p1._api_base == "https://54741234.xyz/antigravity/v1beta"
    assert (
        p1._model_endpoint("generateContent")
        == "https://54741234.xyz/antigravity/v1beta/models/gemini-4-carbon-high:generateContent"
    )

    p2 = GeminiNativeProvider(
        name="g2",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="k",
        model="models/gemini-2.5-pro",
        capabilities=Capabilities(),
    )
    assert p2._api_base == "https://generativelanguage.googleapis.com/v1beta"
    assert (
        p2._model_endpoint("streamGenerateContent")
        == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:streamGenerateContent"
    )


def test_clean_json_schema_for_gemini() -> None:
    raw = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Demo",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "count": {"type": ["integer", "null"], "default": 1, "title": "Count"},
            "empty_obj": {"type": "object", "properties": {}, "required": ["missing"]},
        },
        "required": ["count", "nonexistent"],
    }
    cleaned = _clean_json_schema_for_gemini(raw)
    assert "$schema" not in cleaned
    assert "additionalProperties" not in cleaned
    assert cleaned["required"] == ["count"]
    assert cleaned["properties"]["count"] == {"type": "integer", "nullable": True}
    assert "required" not in cleaned["properties"]["empty_obj"]


def test_coalesce_and_finalize_preserves_thought_and_signatures() -> None:
    prov = _make_provider()
    streamed_parts: List[Dict[str, Any]] = [
        {"thought": True, "text": "Step 1. "},
        {"thought": True, "text": "Step 2."},
        {
            "thoughtSignature": "REAL_SIG_1",
            "functionCall": {"name": "calc", "args": {"expr": "13*17"}, "id": "call_1"},
        },
        {
            "functionCall": {"name": "calc", "args": {"expr": "19*23"}, "id": "call_2"},
        },
        {"text": ""},
    ]
    coalesced = _coalesce_streamed_gemini_parts(streamed_parts)
    assert len(coalesced) == 3
    assert coalesced[0] == {"thought": True, "text": "Step 1. Step 2."}
    assert coalesced[1]["thoughtSignature"] == "REAL_SIG_1"
    assert "thoughtSignature" not in coalesced[2]

    resp = prov._finalize_from_parts(
        streamed_parts,
        finish_reason="STOP",
        usage_meta={
            "promptTokenCount": 100,
            "candidatesTokenCount": 30,
            "thoughtsTokenCount": 50,
            "totalTokenCount": 180,
        },
    )
    assert resp.content is None
    assert resp.reasoning_content == "Step 1. Step 2."
    assert len(resp.tool_calls) == 2
    assert resp.tool_calls[0].extra_content == {
        "google": {"thought_signature": "REAL_SIG_1"}
    }
    assert resp.tool_calls[1].extra_content is None
    assert resp.usage is not None
    assert resp.usage.completion_tokens == 80
    assert resp.gemini_message_content == coalesced


def test_multi_turn_roundtrip_and_fallback_conversion() -> None:
    prov = _make_provider()
    ctx = ConversationContext()
    ctx.add_user_message("算两个乘法")

    # Turn 1: native Gemini response with real parts + signatures
    native_parts = [
        {"thought": True, "text": "Need two calc calls."},
        {
            "thoughtSignature": "REAL_SIG_ABC",
            "functionCall": {"name": "calc", "args": {"expr": "2*3"}, "id": "c1"},
        },
        {
            "functionCall": {"name": "calc", "args": {"expr": "4*5"}, "id": "c2"},
        },
    ]
    ctx.add_assistant_message(
        content=None,
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "calc", "arguments": '{"expr": "2*3"}'},
                "extra_content": {"google": {"thought_signature": "REAL_SIG_ABC"}},
            },
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "calc", "arguments": '{"expr": "4*5"}'},
            },
        ],
        reasoning_content="Need two calc calls.",
        gemini_message_content=native_parts,
    )
    ctx.add_tool_result("c1", '{"result": 6}')
    ctx.add_tool_result("c2", '{"result": 20}')

    # Turn 2: legacy OpenAI-format tool call without gemini_message_content
    ctx.add_user_message("再算一个")
    ctx.add_assistant_message(
        content=None,
        tool_calls=[
            {
                "id": "c3",
                "type": "function",
                "function": {"name": "calc", "arguments": '{"expr": "6*7"}'},
            },
            {
                "id": "c4",
                "type": "function",
                "function": {"name": "calc", "arguments": '{"expr": "8*9"}'},
            },
        ],
    )
    ctx.add_tool_result("c3", "42")
    ctx.add_tool_result("c4", "72")

    norm_msgs, stats = normalize_tool_sequence(ctx.get_messages())
    assert not any(stats.values())

    # Summary sanitizer strips internal gemini_message_content cleanly
    sum_msgs = prepare_messages_for_summary(norm_msgs)
    assert all("gemini_message_content" not in m for m in sum_msgs)

    sys_inst, contents = asyncio.run(prov._convert_messages(norm_msgs, "sys"))
    assert sys_inst == "sys"
    # user -> model(native) -> user(2 functionResponse + next user merged) -> model(fallback) -> user(2 functionResponse)
    assert [c["role"] for c in contents] == ["user", "model", "user", "model", "user"]
    # Native parts preserved verbatim (no dummy signature overwriting REAL_SIG_ABC)
    assert contents[1]["parts"] == native_parts
    # Fallback turn puts dummy signature ONLY on first parallel functionCall
    fb_parts = contents[3]["parts"]
    assert fb_parts[0]["thoughtSignature"] == "skip_thought_signature_validator"
    assert "thoughtSignature" not in fb_parts[1]


def test_stream_retries_on_504_and_stream_timeout_before_visible_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    async def _fast_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(504, text="Gateway Timeout", request=request)
        if calls == 2:
            sse = (
                'data: {"candidates":[{"content":{"role":"model","parts":[{"thought":true,"text":"partial thought"}]}}]}\n\n'
                'data: {"error":{"message":"stream_timeout"}}\n\n'
            )
            return httpx.Response(200, text=sse, request=request)
        sse_ok = (
            'data: {"candidates":[{"content":{"role":"model","parts":[{"thought":true,"text":"fresh thought"}]}}]}\n\n'
            'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"OK!","thoughtSignature":"SIG_FINAL"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":2,"thoughtsTokenCount":3,"totalTokenCount":15}}\n\n'
        )
        return httpx.Response(200, text=sse_ok, request=request)

    prov = _make_provider(stream=True)
    prov._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    visible_deltas: List[str] = []
    resp = asyncio.run(
        prov.chat_with_tools(
            [{"role": "user", "content": "ping"}],
            on_content_delta=visible_deltas.append,
        )
    )
    assert calls == 3
    assert visible_deltas == ["OK!"]
    assert resp.content == "OK!"
    assert resp.reasoning_content == "fresh thought"
    assert resp.gemini_message_content is not None
    assert len(resp.gemini_message_content) == 2


def test_stream_does_not_retry_after_visible_content_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    async def _fast_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        sse = (
            'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Half reply "}]}}]}\n\n'
            'data: {"error":{"message":"stream_timeout"}}\n\n'
        )
        return httpx.Response(200, text=sse, request=request)

    prov = _make_provider(stream=True)
    prov._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    visible_deltas: List[str] = []
    with pytest.raises(RuntimeError, match="stream_timeout"):
        asyncio.run(
            prov.chat_with_tools(
                [{"role": "user", "content": "ping"}],
                on_content_delta=visible_deltas.append,
            )
        )
    assert calls == 1
    assert visible_deltas == ["Half reply "]
