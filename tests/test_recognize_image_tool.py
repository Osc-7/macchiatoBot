"""
RecognizeImageTool 行为测试：
- 拒绝同时 / 完全不传 image_path / image_url
- image_url 支持 http(s) 和 data:
- image_path 支持从 unseen_media 回查
- 未配置 vision_provider 时给出 NO_VISION_PROVIDER 错误
- 调用 chat_with_image 时透传 provider_name=vision_provider
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.llm.response import LLMResponse
from system.tools.recognize_image_tool import RecognizeImageTool


def _make_llm_client(*, vision_provider: str | None):
    client = MagicMock()
    client.vision_provider_name = vision_provider
    client.chat_with_image = AsyncMock(
        return_value=LLMResponse(content="图片里是一行报错", tool_calls=[])
    )
    return client


@pytest.mark.asyncio
async def test_missing_image_inputs_returns_error():
    client = _make_llm_client(vision_provider="vl")
    tool = RecognizeImageTool(llm_client=client, unseen_media=[])
    result = await tool.execute()
    assert result.success is False
    assert result.error == "MISSING_IMAGE"


@pytest.mark.asyncio
async def test_both_inputs_returns_error():
    client = _make_llm_client(vision_provider="vl")
    tool = RecognizeImageTool(llm_client=client, unseen_media=[])
    result = await tool.execute(image_path="a", image_url="https://x/y.png")
    assert result.success is False
    assert result.error == "CONFLICTING_INPUT"


@pytest.mark.asyncio
async def test_no_vision_provider_returns_error():
    client = _make_llm_client(vision_provider=None)
    tool = RecognizeImageTool(llm_client=client, unseen_media=[])
    result = await tool.execute(image_url="https://example.com/a.png")
    assert result.success is False
    assert result.error == "NO_VISION_PROVIDER"
    client.chat_with_image.assert_not_called()


@pytest.mark.asyncio
async def test_image_url_http_success():
    client = _make_llm_client(vision_provider="qwen3vl")
    tool = RecognizeImageTool(llm_client=client, unseen_media=[])
    result = await tool.execute(image_url="https://example.com/a.png", question="看看")
    assert result.success is True
    assert "报错" in result.message
    call_kwargs = client.chat_with_image.await_args.kwargs
    assert call_kwargs["provider_name"] == "qwen3vl"
    assert call_kwargs["image_url"] == "https://example.com/a.png"
    assert call_kwargs["prompt"] == "看看"


@pytest.mark.asyncio
async def test_image_url_data_success():
    client = _make_llm_client(vision_provider="vl")
    tool = RecognizeImageTool(llm_client=client, unseen_media=[])
    data_url = "data:image/png;base64,AAAA"
    result = await tool.execute(image_url=data_url)
    assert result.success is True
    call_kwargs = client.chat_with_image.await_args.kwargs
    assert call_kwargs["image_url"] == data_url


@pytest.mark.asyncio
async def test_image_url_rejects_unknown_scheme():
    client = _make_llm_client(vision_provider="vl")
    tool = RecognizeImageTool(llm_client=client, unseen_media=[])
    result = await tool.execute(image_url="ftp://example.com/a.png")
    assert result.success is False
    assert result.error == "INVALID_URL"


@pytest.mark.asyncio
async def test_image_path_lookup_by_name_from_unseen_media():
    client = _make_llm_client(vision_provider="vl")
    unseen = [
        {
            "name": "image_1",
            "path": "",
            "url": "data:image/png;base64,ZZZZ",
            "media_type": "image",
        }
    ]
    tool = RecognizeImageTool(llm_client=client, unseen_media=unseen)
    result = await tool.execute(image_path="image_1")
    assert result.success is True
    call_kwargs = client.chat_with_image.await_args.kwargs
    assert call_kwargs["image_url"] == "data:image/png;base64,ZZZZ"


@pytest.mark.asyncio
async def test_empty_description_returns_error():
    client = _make_llm_client(vision_provider="vl")
    client.chat_with_image = AsyncMock(
        return_value=LLMResponse(content="   ", tool_calls=[])
    )
    tool = RecognizeImageTool(llm_client=client, unseen_media=[])
    result = await tool.execute(image_url="https://a.example/x.png")
    assert result.success is False
    assert result.error == "EMPTY_DESCRIPTION"
