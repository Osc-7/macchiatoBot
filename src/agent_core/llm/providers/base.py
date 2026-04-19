"""
BaseProvider 抽象接口。

每个 LLM provider 实例对应「一个 provider 名 + 一组连接参数 + 一个具体模型」，
在同一 AgentCore 生命周期内长期存活（底层 HTTP 客户端常驻）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from agent_core.llm.capabilities import Capabilities
from agent_core.llm.response import LLMResponse


class BaseProvider(ABC):
    """所有 LLM provider 适配器的公共接口。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """provider 注册名（对应 config.llm.providers 的 key）。"""

    @property
    @abstractmethod
    def model(self) -> str:
        """provider 后端使用的模型名。"""

    @property
    @abstractmethod
    def capabilities(self) -> Capabilities:
        """此 provider 的能力矩阵。"""

    @property
    @abstractmethod
    def context_window(self) -> int:
        """provider 的上下文窗口 token 数。"""

    @property
    @abstractmethod
    def temperature(self) -> float:
        """采样温度。"""

    @property
    @abstractmethod
    def max_tokens(self) -> int:
        """单次响应最大输出 token 数。"""

    @abstractmethod
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        system_message: Optional[str] = None,
    ) -> LLMResponse:
        """不带工具的基础对话。"""

    @abstractmethod
    async def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        tool_choice: str = "auto",
        on_content_delta: Optional[Callable[[str], Any]] = None,
        on_reasoning_delta: Optional[Callable[[str], Any]] = None,
        max_tokens_override: Optional[int] = None,
    ) -> LLMResponse:
        """
        支持工具调用的对话（主循环使用）。

        ``max_tokens_override`` 不为 ``None`` 时覆盖构造期固定的 ``max_tokens``，
        供 AgentCore 在调用前按「context_window − estimated_prompt − safety_margin」
        动态收紧 completion 预算，避免常量 ``max_tokens`` 把窗口顶爆。
        """

    @abstractmethod
    async def chat_with_image(
        self,
        prompt: str,
        image_url: str,
        system_message: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> LLMResponse:
        """多模态识图（仅当 capabilities.vision == True 才应被调用）。"""

    @abstractmethod
    async def close(self) -> None:
        """释放底层连接。"""
