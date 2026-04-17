"""
非多模态模型 + image_url：OpenAI 等平台通常返回 400（本文件用 mock 固定行为，便于回归）。
真实探测见 devtools/probe_openai_text_model_with_image.py（需 OPENAI_API_KEY）。
"""

import httpx
import pytest
from unittest.mock import AsyncMock, patch

from openai import BadRequestError


@pytest.mark.asyncio
async def test_openai_style_400_when_text_model_gets_image_url():
    """模拟：纯文本模型收到 image_url → BadRequestError，业务可据此做 fallback。"""
    from agent_core.llm.client import LLMClient
    from agent_core.config import Config, LLMConfig

    cfg = Config(
        llm=LLMConfig(
            api_key="k",
            model="gpt-3.5-turbo",
            base_url="https://api.openai.com/v1",
            stream=False,
        )
    )

    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    resp = httpx.Response(400, request=req)
    err = BadRequestError(
        "model does not support image message content types",
        response=resp,
        body={
            "error": {
                "message": "Invalid content: image_url is not supported by this model.",
                "type": "invalid_request_error",
            }
        },
    )

    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI") as mock_cls:
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=err)
        mock_client.close = AsyncMock()
        mock_cls.return_value = mock_client

        client = LLMClient(config=cfg)
        for provider in client._providers.values():
            provider._client = mock_client

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/a.jpg"},
                    },
                ],
            }
        ]

        with pytest.raises(BadRequestError):
            await client.chat_with_tools(messages=messages, tools=None)
