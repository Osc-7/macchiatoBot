"""
LLM 客户端封装

封装豆包/阿里云百炼 Qwen/OpenAI 兼容 API 调用，支持多轮工具调用（Function Calling）。
百炼说明：https://bailian.console.aliyun.com/ 支持 qwen-3.5-plus 等多轮工具调用。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from schedule_agent.config import Config, get_config


@dataclass
class TokenUsage:
    """单次调用的 token 用量"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_response(cls, response: Any) -> "TokenUsage":
        """从 API 响应中解析 usage，无则返回全 0"""
        if response is None:
            return cls()
        usage = getattr(response, "usage", None)
        if usage is None:
            return cls()
        return cls(
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
        )


@dataclass
class ToolCall:
    """工具调用"""

    id: str
    """工具调用 ID"""

    name: str
    """工具名称"""

    arguments: Dict[str, Any]
    """工具参数"""


@dataclass
class LLMResponse:
    """LLM 响应"""

    content: Optional[str]
    """文本内容"""

    tool_calls: List[ToolCall] = field(default_factory=list)
    """工具调用列表"""

    finish_reason: str = "stop"
    """结束原因"""

    raw_response: Any = None
    """原始响应对象"""

    usage: Optional[TokenUsage] = None
    """本次调用的 token 用量（API 返回时才有）"""


class LLMClient:
    """
    LLM 客户端。

    封装豆包/阿里云百炼 Qwen（OpenAI 兼容）/OpenAI API 调用，支持：
    - 基础对话
    - 多轮工具调用（Function Calling）
    - 流式响应（可选）
    """

    def __init__(self, config: Optional[Config] = None):
        """
        初始化 LLM 客户端。

        Args:
            config: 配置对象，如果为 None 则使用全局配置
        """
        self._config = config or get_config()
        self._client = AsyncOpenAI(
            api_key=self._config.llm.api_key,
            base_url=self._config.llm.base_url,
        )

    @property
    def model(self) -> str:
        """获取模型名称"""
        return self._config.llm.model

    @property
    def temperature(self) -> float:
        """获取温度参数"""
        return self._config.llm.temperature

    @property
    def max_tokens(self) -> int:
        """获取最大 token 数"""
        return self._config.llm.max_tokens

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        system_message: Optional[str] = None,
    ) -> LLMResponse:
        """
        发送基础对话请求（无工具）。

        Args:
            messages: 消息列表
            system_message: 系统消息（可选）

        Returns:
            LLM 响应
        """
        full_messages = []

        if system_message:
            full_messages.append({"role": "system", "content": system_message})

        full_messages.extend(messages)

        response = await self._client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        choice = response.choices[0]
        usage = TokenUsage.from_response(response)

        return LLMResponse(
            content=choice.message.content,
            tool_calls=[],
            finish_reason=choice.finish_reason,
            raw_response=response,
            usage=usage,
        )

    async def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        """
        发送支持工具调用的对话请求。

        Args:
            messages: 消息列表
            tools: 工具定义列表（OpenAI Function Calling 格式）
            system_message: 系统消息
            tool_choice: 工具选择策略 ("auto", "none", "required", 或具体工具名)

        Returns:
            LLM 响应
        """
        full_messages = []

        if system_message:
            full_messages.append({"role": "system", "content": system_message})

        full_messages.extend(messages)

        request_params = {
            "model": self.model,
            "messages": full_messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        if tools:
            request_params["tools"] = tools
            request_params["tool_choice"] = tool_choice
            # 百炼/OpenAI 兼容：parallel_tool_calls 让模型单次返回多个工具调用，减少往返
            # 参见 https://help.aliyun.com/zh/model-studio/qwen-function-calling
            request_params["parallel_tool_calls"] = True

        response = await self._client.chat.completions.create(**request_params)

        choice = response.choices[0]

        # 解析工具调用
        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                    )
                )

        usage = TokenUsage.from_response(response)

        return LLMResponse(
            content=choice.message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            raw_response=response,
            usage=usage,
        )

    async def close(self) -> None:
        """关闭客户端连接"""
        await self._client.close()
