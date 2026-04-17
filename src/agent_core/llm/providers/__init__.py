"""
LLM Provider 适配层。

每个 provider 独立声明能力（Capabilities）并实现与厂商端点的直接交互。
LLMClient 持有多个 provider 并在运行时做路由。
"""

from .base import BaseProvider
from .openai_compat import OpenAICompatProvider
from .anthropic_compat import AnthropicCompatProvider

__all__ = ["BaseProvider", "OpenAICompatProvider", "AnthropicCompatProvider"]
