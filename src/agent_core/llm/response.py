"""
LLM 响应数据类。

从 client.py 拆出来：LLMResponse / ToolCall / TokenUsage，避免循环依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union


@dataclass
class TokenUsage:
    """单次调用的 token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    #: DeepSeek 等：输入侧 KV 缓存命中 token（未返回时为 0）
    prompt_cache_hit_tokens: int = 0
    #: DeepSeek 等：输入侧未命中缓存 token（未返回时为 0）
    prompt_cache_miss_tokens: int = 0

    @classmethod
    def from_response(cls, response: Any) -> "TokenUsage":
        """从 API 响应中解析 usage；无则返回全 0。"""
        if response is None:
            return cls()
        usage = getattr(response, "usage", None)
        if usage is None:
            return cls()
        return cls.from_usage(usage)

    @classmethod
    def from_usage(cls, usage: Any) -> "TokenUsage":
        """直接从 usage 对象解析；无则返回全 0。"""
        if usage is None:
            return cls()
        hit = getattr(usage, "prompt_cache_hit_tokens", None)
        miss = getattr(usage, "prompt_cache_miss_tokens", None)
        return cls(
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
            prompt_cache_hit_tokens=int(hit) if hit is not None else 0,
            prompt_cache_miss_tokens=int(miss) if miss is not None else 0,
        )


@dataclass
class ToolCall:
    """工具调用。"""

    id: str
    """工具调用 ID。"""

    name: str
    """工具名称。"""

    arguments: Union[Dict[str, Any], str]
    """工具参数；通常为 dict。流式解析失败时可能为原始 JSON 字符串，由执行层尝试解析或返回错误。"""

    extra_content: Optional[Dict[str, Any]] = None
    """厂商扩展字段。OpenAI 兼容层里常见 ``extra_content.google.thought_signature``（Gemini、Kimi 等），多轮工具须原样回传，否则 400。无签名时（跨模型/中途切换）Gemini 允许使用官方 dummy，见 thought-signatures 文档。"""


@dataclass
class LLMResponse:
    """LLM 响应。"""

    content: Optional[str]
    """文本内容。"""

    tool_calls: List[ToolCall] = field(default_factory=list)
    """工具调用列表。"""

    finish_reason: str = "stop"
    """结束原因。"""

    raw_response: Any = None
    """原始响应对象（可选）。"""

    usage: Optional[TokenUsage] = None
    """本次调用的 token 用量（API 返回时才有）。"""

    reasoning_content: Optional[str] = None
    """模型返回的推理/思考文本（如 Kimi/GLM 的 reasoning_content，多轮需回传）。"""

    anthropic_message_content: Optional[List[Dict[str, Any]]] = None
    """Anthropic Messages API 返回的 content 块列表（thinking/signature/text/tool_use）。

    扩展思考 + 多轮工具调用时，Kimi 等端点要求下一轮请求**原样**回传这些块；
    仅靠 OpenAI 风格的 content + tool_calls 重建会丢失 thinking/signature，导致 400。
    """
