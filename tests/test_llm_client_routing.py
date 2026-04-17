"""
LLMClient 多 provider 路由 / runtime switch_model / vision_provider 回落测试。

测试策略：
- 通过 config.yaml 新风格的 providers map 构造多 provider 场景
- patch `agent_core.llm.providers.openai_compat.AsyncOpenAI`，底层连接是 mock
- 断言 active provider 切换后 chat_with_tools 走到对应 model，并且 chat_with_image
  默认走 vision_provider 而不是 active provider
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.config import (
    CapabilitiesModel,
    Config,
    LLMConfig,
    ProviderEntry,
)
from agent_core.llm import LLMClient


def _make_config() -> Config:
    return Config(
        llm=LLMConfig(
            api_key="legacy-k",
            model="legacy-m",
            providers={
                "deepseek": ProviderEntry(
                    base_url="https://example.com/v1",
                    api_key="k-deepseek",
                    model="deepseek-chat",
                    capabilities=CapabilitiesModel(vision=False),
                ),
                "qwen3vl": ProviderEntry(
                    base_url="https://example.com/v1",
                    api_key="k-qwen3vl",
                    model="qwen3-vl",
                    capabilities=CapabilitiesModel(vision=True),
                ),
            },
            active="deepseek",
            vision_provider="qwen3vl",
        )
    )


def _mock_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.choices[0].message.tool_calls = None
    resp.choices[0].finish_reason = "stop"
    resp.usage = None
    return resp


def test_llm_client_initializes_providers():
    cfg = _make_config()
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI"):
        client = LLMClient(config=cfg)
    assert set(client.providers.keys()) == {"deepseek", "qwen3vl"}
    assert client.active_provider_name == "deepseek"
    assert client.vision_provider_name == "qwen3vl"
    assert client.capabilities.vision is False
    assert client.model == "deepseek-chat"


def test_list_models_returns_capabilities():
    cfg = _make_config()
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI"):
        client = LLMClient(config=cfg)
    names = [name for name, _ in client.list_models()]
    assert names == ["deepseek", "qwen3vl"]
    caps_map = dict(client.list_models())
    assert caps_map["deepseek"].vision is False
    assert caps_map["qwen3vl"].vision is True


def test_switch_model_changes_active_and_caps():
    cfg = _make_config()
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI"):
        client = LLMClient(config=cfg)
    client.switch_model("qwen3vl")
    assert client.active_provider_name == "qwen3vl"
    assert client.capabilities.vision is True
    assert client.model == "qwen3-vl"


def test_switch_model_rejects_unknown():
    cfg = _make_config()
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI"):
        client = LLMClient(config=cfg)
    with pytest.raises(ValueError):
        client.switch_model("unknown-provider")


def test_vision_provider_autopick_when_missing():
    cfg = Config(
        llm=LLMConfig(
            providers={
                "deepseek": ProviderEntry(
                    base_url="https://example.com/v1",
                    api_key="k",
                    model="deepseek",
                    capabilities=CapabilitiesModel(vision=False),
                ),
                "vl": ProviderEntry(
                    base_url="https://example.com/v1",
                    api_key="k",
                    model="vl-xl",
                    capabilities=CapabilitiesModel(vision=True),
                ),
            },
            active="deepseek",
        )
    )
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI"):
        client = LLMClient(config=cfg)
    assert client.vision_provider_name == "vl"


@pytest.mark.asyncio
async def test_chat_with_tools_uses_active_provider_model():
    cfg = _make_config()
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_response("从 deepseek")
        )
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        client = LLMClient(config=cfg)
        for provider in client._providers.values():
            provider._client = mock_client

        response = await client.chat_with_tools(
            messages=[{"role": "user", "content": "hi"}], tools=None
        )
        assert response.content == "从 deepseek"
        assert mock_client.chat.completions.create.call_args.kwargs["model"] == (
            "deepseek-chat"
        )

        client.switch_model("qwen3vl")
        await client.chat_with_tools(
            messages=[{"role": "user", "content": "hi"}], tools=None
        )
        assert mock_client.chat.completions.create.call_args.kwargs["model"] == (
            "qwen3-vl"
        )


@pytest.mark.asyncio
async def test_chat_with_image_routes_to_vision_provider_by_default():
    cfg = _make_config()
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_response("图片描述")
        )
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        client = LLMClient(config=cfg)
        for provider in client._providers.values():
            provider._client = mock_client

        response = await client.chat_with_image(
            prompt="描述图片",
            image_url="https://example.com/a.png",
        )
        assert response.content == "图片描述"
        assert mock_client.chat.completions.create.call_args.kwargs["model"] == (
            "qwen3-vl"
        )


@pytest.mark.asyncio
async def test_chat_with_image_provider_name_overrides_default():
    cfg = _make_config()
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_response("from deepseek - but wrong")
        )
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        client = LLMClient(config=cfg)
        for provider in client._providers.values():
            provider._client = mock_client

        await client.chat_with_image(
            prompt="描述",
            image_url="https://example.com/x.png",
            provider_name="deepseek",
        )
        assert mock_client.chat.completions.create.call_args.kwargs["model"] == (
            "deepseek-chat"
        )
