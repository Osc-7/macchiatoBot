"""
OpenAICompatProvider 单测：验证 chat / chat_with_tools / chat_with_image / close
的请求参数组装及对 vendor_params / capabilities 的处理。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.llm.capabilities import Capabilities
from agent_core.llm.providers import OpenAICompatProvider


def _mock_openai_chat_completion_response(content: str = "ok") -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.choices[0].message.tool_calls = None
    resp.choices[0].finish_reason = "stop"
    resp.usage = None
    return resp


def _make_provider(**overrides) -> OpenAICompatProvider:
    kwargs = dict(
        name="default",
        base_url="https://example.com/v1",
        api_key="sk-x",
        model="gpt-4o-mini",
        capabilities=Capabilities(vision=False),
        temperature=0.5,
        max_tokens=1024,
        request_timeout_seconds=30.0,
        stream=False,
        vendor_params={},
    )
    kwargs.update(overrides)
    return OpenAICompatProvider(**kwargs)


@pytest.mark.asyncio
async def test_chat_sends_system_and_user_messages():
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_openai_chat_completion_response("hi back")
        )
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = _make_provider()
        response = await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            system_message="you are helpful",
        )

        assert response.content == "hi back"
        call = mock_client.chat.completions.create.call_args.kwargs
        assert call["model"] == "gpt-4o-mini"
        assert call["messages"][0] == {"role": "system", "content": "you are helpful"}
        assert call["messages"][1]["role"] == "user"


@pytest.mark.asyncio
async def test_chat_with_tools_respects_parallel_tool_calls_cap():
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_openai_chat_completion_response()
        )
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = _make_provider(
            capabilities=Capabilities(parallel_tool_calls=False),
        )
        tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
        await provider.chat_with_tools(
            messages=[{"role": "user", "content": "hi"}], tools=tools
        )
        # parallel_tool_calls 不应出现在 kwargs 中
        assert "parallel_tool_calls" not in mock_client.chat.completions.create.call_args.kwargs


@pytest.mark.asyncio
async def test_chat_with_tools_max_tokens_override_takes_effect():
    """``max_tokens_override`` 应覆盖构造期 ``max_tokens``，供 AgentCore 自适应收紧。"""
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_openai_chat_completion_response()
        )
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = _make_provider(max_tokens=65536)
        await provider.chat_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens_override=8192,
        )
        assert mock_client.chat.completions.create.call_args.kwargs["max_tokens"] == 8192

        # None / 0 / 负值 → 回退到构造期固定值，不污染请求
        await provider.chat_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens_override=None,
        )
        assert mock_client.chat.completions.create.call_args.kwargs["max_tokens"] == 65536
        await provider.chat_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens_override=0,
        )
        assert mock_client.chat.completions.create.call_args.kwargs["max_tokens"] == 65536


@pytest.mark.asyncio
async def test_vendor_params_forwarded_as_extra_body():
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_openai_chat_completion_response()
        )
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        vp = {"enable_search": True, "search_options": {"forced_search": True}}
        provider = _make_provider(vendor_params=vp)
        await provider.chat_with_tools(
            messages=[{"role": "user", "content": "hi"}], tools=None
        )
        call = mock_client.chat.completions.create.call_args.kwargs
        assert call["extra_body"] == vp


@pytest.mark.asyncio
async def test_chat_with_image_builds_multimodal_user_message():
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_openai_chat_completion_response("图片里是一个 logo")
        )
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = _make_provider(capabilities=Capabilities(vision=True))
        response = await provider.chat_with_image(
            prompt="描述图片",
            image_url="https://example.com/a.png",
            model_override="qwen-vl-plus",
        )
        assert response.content == "图片里是一个 logo"
        call = mock_client.chat.completions.create.call_args.kwargs
        assert call["model"] == "qwen-vl-plus"
        user = call["messages"][-1]
        assert user["role"] == "user"
        assert user["content"][0]["type"] == "text"
        assert user["content"][1]["type"] == "image_url"
        assert user["content"][1]["image_url"]["url"] == "https://example.com/a.png"


@pytest.mark.asyncio
async def test_close_closes_underlying_client():
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        provider = _make_provider()
        await provider.close()
        mock_client.close.assert_awaited()
