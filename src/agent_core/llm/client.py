"""
LLM 客户端：多 provider 持有者 + 路由器。

LLMClient 不再直接调用厂商 SDK，而是持有一张 name -> BaseProvider 的表，
由 active_name 指向当前主对话 provider。运行时 `/model <name>` 命令切换时，
只修改 active_name，底层连接池常驻，避免重建。

多模态识图固定走 vision_provider（由 config.llm.vision_provider 指定；未指定时
自动挑第一个 capabilities.vision=True 的 provider）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent_core.config import Config, get_config
from agent_core.llm.capabilities import Capabilities
from agent_core.llm.provider_resolve import resolve_llm_provider_key
from agent_core.llm.providers import (
    AnthropicCompatProvider,
    BaseProvider,
    OpenAICompatProvider,
)
from agent_core.llm.providers.openai_compat import (
    _strip_thinking_content,
    get_context_window_tokens_for_model,
)
from agent_core.llm.response import LLMResponse, ToolCall, TokenUsage

logger = logging.getLogger(__name__)


def _build_provider_from_entry(
    name: str,
    entry: Any,
    *,
    llm_config: Any,
) -> BaseProvider:
    """从 LLMConfig.ProviderEntry 构造 provider 实例。
    
    根据 entry.protocol 字段选择 provider 类型：
    - "openai" 或未指定：OpenAICompatProvider
    - "anthropic": AnthropicCompatProvider
    """
    caps_src = getattr(entry, "capabilities", None)
    caps = Capabilities(
        vision=getattr(caps_src, "vision", False) if caps_src is not None else False,
        function_calling=(
            getattr(caps_src, "function_calling", True) if caps_src is not None else True
        ),
        parallel_tool_calls=(
            getattr(caps_src, "parallel_tool_calls", True)
            if caps_src is not None
            else True
        ),
        reasoning_content=(
            getattr(caps_src, "reasoning_content", False)
            if caps_src is not None
            else False
        ),
        thinking_tag_inline=(
            getattr(caps_src, "thinking_tag_inline", False)
            if caps_src is not None
            else False
        ),
        context_window=(
            getattr(caps_src, "context_window", None) if caps_src is not None else None
        ),
    )

    vendor_params = dict(getattr(entry, "vendor_params", {}) or {})
    headers = dict(getattr(entry, "headers", {}) or {})

    entry_temp = getattr(entry, "temperature", None)
    if entry_temp is not None:
        temperature = float(entry_temp)
    else:
        temperature = float(getattr(llm_config, "temperature", 0.7))
    max_tokens = int(getattr(llm_config, "max_tokens", 4096))
    request_timeout_seconds = float(getattr(llm_config, "request_timeout_seconds", 120.0))
    stream = bool(getattr(llm_config, "stream", False))
    
    # 根据 protocol 字段选择 provider 类型
    protocol = getattr(entry, "protocol", None)
    if protocol == "anthropic":
        return AnthropicCompatProvider(
            name=name,
            base_url=str(entry.base_url),
            api_key=str(entry.api_key),
            model=str(entry.model),
            capabilities=caps,
            temperature=temperature,
            max_tokens=max_tokens,
            request_timeout_seconds=request_timeout_seconds,
            stream=stream,
            vendor_params=vendor_params,
            headers=headers,
        )
    else:
        # 默认使用 OpenAI 兼容协议
        return OpenAICompatProvider(
            name=name,
            base_url=str(entry.base_url),
            api_key=str(entry.api_key),
            model=str(entry.model),
            capabilities=caps,
            temperature=temperature,
            max_tokens=max_tokens,
            request_timeout_seconds=request_timeout_seconds,
            stream=stream,
            vendor_params=vendor_params,
            headers=headers,
        )


class LLMClient:
    """
    LLM 客户端路由器。

    用法：
        client = LLMClient(config=cfg)
        await client.chat_with_tools(messages, tools=tools)   # 主 provider
        client.switch_model("qwen3vl")
        await client.chat_with_image(prompt, image_url)       # 走 vision_provider
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        model_override: Optional[str] = None,
    ) -> None:
        self._config = config or get_config()
        self._model_override = model_override
        self._providers: Dict[str, BaseProvider] = {}

        llm_cfg = self._config.llm
        providers_map = getattr(llm_cfg, "providers", {}) or {}
        if not providers_map:
            raise RuntimeError(
                "LLMConfig.providers 为空：请在 config.yaml 中配置至少一个 provider，"
                "或使用老字段 (model/base_url/api_key)，config.py 会自动迁移到 "
                "providers['default']。"
            )

        for name, entry in providers_map.items():
            self._providers[str(name)] = _build_provider_from_entry(
                str(name), entry, llm_config=llm_cfg
            )

        active_name = getattr(llm_cfg, "active", None)
        if active_name and active_name in self._providers:
            self._active: str = str(active_name)
        else:
            self._active = next(iter(self._providers.keys()))

        self._vision_provider_name: Optional[str] = self._resolve_vision_provider(
            getattr(llm_cfg, "vision_provider", None)
        )

    def _resolve_vision_provider(self, configured: Optional[str]) -> Optional[str]:
        """返回用作 chat_with_image 的 provider 名；未显式配置时自动挑首个 vision=True。"""
        if configured and configured in self._providers:
            return str(configured)
        if configured:
            logger.warning(
                "vision_provider=%s 未在 providers 中找到，忽略", configured
            )
        for name, provider in self._providers.items():
            if provider.capabilities.vision:
                return name
        return None

    def _active_provider(self) -> BaseProvider:
        return self._providers[self._active]

    @property
    def active_provider_name(self) -> str:
        return self._active

    @property
    def vision_provider_name(self) -> Optional[str]:
        return self._vision_provider_name

    @property
    def providers(self) -> Dict[str, BaseProvider]:
        return dict(self._providers)

    @property
    def capabilities(self) -> Capabilities:
        """当前 active provider 的能力矩阵。"""
        return self._active_provider().capabilities

    @property
    def model(self) -> str:
        """model_override 优先（用于总结等轻量任务），否则取 active provider 的 model。"""
        return self._model_override or self._active_provider().model

    @property
    def temperature(self) -> float:
        return self._active_provider().temperature

    @property
    def max_tokens(self) -> int:
        return self._active_provider().max_tokens

    @property
    def context_window(self) -> int:
        """上下文窗口 token 数：优先看 active provider 的显式声明，再按模型名启发式。"""
        provider = self._active_provider()
        cw = provider.capabilities.context_window
        if cw is not None:
            return cw
        return get_context_window_tokens_for_model(self.model)

    def switch_model(self, name: str) -> None:
        """
        在会话内切换主对话 provider。不重建底层 HTTP 客户端。

        Args:
            name: provider 注册名、YAML ``label`` 或 ``model``（API ID），见
                ``resolve_llm_provider_key``。
        """
        key = resolve_llm_provider_key(self._config.llm, name)
        if key not in self._providers:
            raise ValueError(
                f"未知 provider: {name}；已注册：{list(self._providers.keys())}"
            )
        self._active = key
        prov_map = getattr(self._config.llm, "providers", None) or {}
        entry = prov_map.get(key)
        base_url = str(getattr(entry, "base_url", "") or "") if entry is not None else ""
        api_model = str(getattr(entry, "model", "") or "") if entry is not None else ""
        logger.info(
            "LLMClient 主 provider 切换到: query=%r -> key=%s api_model=%s base_url=%s",
            name,
            key,
            api_model,
            base_url,
        )

    def list_models(self) -> List[Tuple[str, Capabilities]]:
        """列出所有已注册 provider 的 (name, capabilities)，按注册顺序。"""
        return [(name, p.capabilities) for name, p in self._providers.items()]

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        system_message: Optional[str] = None,
    ) -> LLMResponse:
        """不带工具的基础对话，走当前 active provider。"""
        return await self._active_provider().chat(
            messages=messages, system_message=system_message
        )

    async def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        tool_choice: str = "auto",
        on_content_delta: Optional[Callable[[str], Any]] = None,
        on_reasoning_delta: Optional[Callable[[str], Any]] = None,
    ) -> LLMResponse:
        """支持工具调用的对话，走当前 active provider。"""
        return await self._active_provider().chat_with_tools(
            messages=messages,
            tools=tools,
            system_message=system_message,
            tool_choice=tool_choice,
            on_content_delta=on_content_delta,
            on_reasoning_delta=on_reasoning_delta,
        )

    async def chat_with_image(
        self,
        prompt: str,
        image_url: str,
        system_message: Optional[str] = None,
        model_override: Optional[str] = None,
        provider_name: Optional[str] = None,
    ) -> LLMResponse:
        """
        多模态识图：默认走 vision_provider；可通过 provider_name 显式指定。

        Args:
            provider_name: 若指定则强制使用该 provider；否则用 vision_provider；
                           若都缺省，退回 active provider。
        """
        chosen = provider_name or self._vision_provider_name or self._active
        if chosen not in self._providers:
            raise ValueError(f"未知 provider: {chosen}")
        provider = self._providers[chosen]
        return await provider.chat_with_image(
            prompt=prompt,
            image_url=image_url,
            system_message=system_message,
            model_override=model_override,
        )

    async def close(self) -> None:
        """关闭所有 provider 的底层连接。"""
        for provider in self._providers.values():
            try:
                await provider.close()
            except Exception:
                logger.exception("关闭 provider %s 失败", provider.name)


__all__ = [
    "LLMClient",
    "LLMResponse",
    "ToolCall",
    "TokenUsage",
    "Capabilities",
    "get_context_window_tokens_for_model",
    "_strip_thinking_content",
]
