"""
LLM 客户端 - 路由器 + 多 provider。

当前唯一 provider 实现：OpenAICompatProvider（见 providers/openai_compat.py）。
vendor_params 在每个 provider 下独立声明，按 OpenAI SDK 的 extra_body 下发。
"""

from .capabilities import Capabilities
from .client import (
    LLMClient,
    get_context_window_tokens_for_model,
)
from .providers import BaseProvider, OpenAICompatProvider
from .response import LLMResponse, TokenUsage, ToolCall
from .provider_resolve import resolve_llm_provider_key

__all__ = [
    "LLMClient",
    "LLMResponse",
    "ToolCall",
    "TokenUsage",
    "Capabilities",
    "BaseProvider",
    "OpenAICompatProvider",
    "get_context_window_tokens_for_model",
    "resolve_llm_provider_key",
]
