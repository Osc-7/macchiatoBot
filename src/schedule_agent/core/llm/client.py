"""
LLM 客户端封装

封装豆包/阿里云百炼 Qwen/OpenAI 兼容 API 调用，支持多轮工具调用（Function Calling）。
百炼说明：https://bailian.console.aliyun.com/ 支持 qwen-3.5-plus 等多轮工具调用。
"""

import json
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from openai import AsyncOpenAI

from schedule_agent.config import Config, get_config

# Qwen 深度思考模式会将推理内容放在 content 中（有时与回复混合），用 ` <think>...</think>` 包裹。
# 参见 https://www.alibabacloud.com/help/zh/model-studio/deep-thinking
THINKING_END_TAG = "</think>"


def _strip_thinking_content(content: Optional[str]) -> Optional[str]:
    """
    剥离 Qwen 等模型的思考内容（` <think>...</think>` 块），只保留 `</think>` 之后的正式回复。

    Args:
        content: 原始 content，可能包含思考内容

    Returns:
        剥离后的 content，无 ` <think>` 块则原样返回
    """
    if not content or not isinstance(content, str):
        return content
    idx = content.find(THINKING_END_TAG)
    if idx == -1:
        return content
    return content[idx + len(THINKING_END_TAG) :].strip()


def _normalize_text_content(content: Any) -> Optional[str]:
    """将模型返回的 content 统一为纯文本。"""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: List[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    texts.append(text)
        if texts:
            return "\n".join(texts).strip()
    return str(content)


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

    @classmethod
    def from_usage(cls, usage: Any) -> "TokenUsage":
        """从 usage 对象直接解析 token 用量，无则返回全 0"""
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

    def __init__(self, config: Optional[Config] = None, model_override: Optional[str] = None):
        """
        初始化 LLM 客户端。

        Args:
            config: 配置对象，如果为 None 则使用全局配置
            model_override: 模型覆盖，若设置则替代 config.llm.model（用于总结等轻量任务）
        """
        self._config = config or get_config()
        self._model_override = model_override
        self._client = AsyncOpenAI(
            api_key=self._config.llm.api_key,
            base_url=self._config.llm.base_url,
            timeout=self._config.llm.request_timeout_seconds,
        )

    @property
    def model(self) -> str:
        """获取模型名称"""
        return self._model_override or self._config.llm.model

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

        request_params = {
            "model": self.model,
            "messages": full_messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        extra_body = self._build_qwen_extra_body()
        if extra_body:
            request_params["extra_body"] = extra_body

        if self._config.llm.stream:
            return await self._chat_with_tools_stream(request_params)

        response = await self._client.chat.completions.create(**request_params)

        choice = response.choices[0]
        usage = TokenUsage.from_response(response)
        content = _strip_thinking_content(_normalize_text_content(choice.message.content))

        return LLMResponse(
            content=content,
            tool_calls=[],
            finish_reason=choice.finish_reason,
            raw_response=response,
            usage=usage,
        )

    async def chat_with_image(
        self,
        prompt: str,
        image_url: str,
        system_message: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> LLMResponse:
        """
        发送多模态识图请求（文本 + 图片）。

        Args:
            prompt: 图片分析提示词
            image_url: 图片地址，支持 http(s) URL 或 data URL
            system_message: 可选系统提示词
            model_override: 可选模型覆盖

        Returns:
            LLM 响应
        """
        full_messages: List[Dict[str, Any]] = []

        if system_message:
            full_messages.append({"role": "system", "content": system_message})

        full_messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        )

        request_params: Dict[str, Any] = {
            "model": model_override or self.model,
            "messages": full_messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        response = await self._client.chat.completions.create(**request_params)
        choice = response.choices[0]
        usage = TokenUsage.from_response(response)
        content = _strip_thinking_content(_normalize_text_content(choice.message.content))

        return LLMResponse(
            content=content,
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
        on_content_delta: Optional[Callable[[str], Any]] = None,
        on_reasoning_delta: Optional[Callable[[str], Any]] = None,
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

        # 百炼扩展参数（通过 extra_body 传递）
        # 参见 https://help.aliyun.com/zh/model-studio/deep-thinking
        extra_body = self._build_qwen_extra_body()
        if extra_body:
            request_params["extra_body"] = extra_body

        if self._config.llm.stream:
            return await self._chat_with_tools_stream(
                request_params,
                on_content_delta=on_content_delta,
                on_reasoning_delta=on_reasoning_delta,
            )

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
        content = _strip_thinking_content(_normalize_text_content(choice.message.content))

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            raw_response=response,
            usage=usage,
        )

    async def _chat_with_tools_stream(
        self,
        request_params: Dict[str, Any],
        on_content_delta: Optional[Callable[[str], Any]] = None,
        on_reasoning_delta: Optional[Callable[[str], Any]] = None,
    ) -> LLMResponse:
        """
        流式调用（网页抓取必须使用流式模式）。
        汇总流式响应后返回完整的 LLMResponse。
        """
        params = {
            **request_params,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        stream = await self._client.chat.completions.create(**params)

        content_parts: List[str] = []
        tool_calls_map: Dict[int, Dict[str, Any]] = {}
        finish_reason = "stop"
        last_usage = None
        filter_state = {"mode": "normal", "pending": ""}

        async for chunk in stream:
            if not chunk.choices:
                if hasattr(chunk, "usage") and chunk.usage:
                    last_usage = chunk.usage
                continue

            delta = chunk.choices[0].delta
            if hasattr(chunk.choices[0], "finish_reason") and chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason

            if delta.content:
                content_parts.append(delta.content)
                if on_content_delta:
                    filtered = self._filter_thinking_delta(delta.content, filter_state)
                    if filtered:
                        maybe_awaitable = on_content_delta(filtered)
                        if inspect.isawaitable(maybe_awaitable):
                            await maybe_awaitable
            if (
                on_reasoning_delta
                and hasattr(delta, "reasoning_content")
                and delta.reasoning_content
            ):
                maybe_awaitable = on_reasoning_delta(delta.reasoning_content)
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable

            if hasattr(delta, "tool_calls") and delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = getattr(tc, "index", 0)
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {
                            "id": getattr(tc, "id", "") or "",
                            "name": getattr(tc.function, "name", "") or "",
                            "arguments": getattr(tc.function, "arguments", "") or "",
                        }
                    else:
                        if getattr(tc, "id", None):
                            tool_calls_map[idx]["id"] = tc.id
                        if hasattr(tc, "function") and tc.function:
                            if getattr(tc.function, "name", None):
                                tool_calls_map[idx]["name"] = tc.function.name
                            if getattr(tc.function, "arguments", None):
                                tool_calls_map[idx]["arguments"] += tc.function.arguments or ""

            if hasattr(chunk, "usage") and chunk.usage:
                last_usage = chunk.usage

        raw_content = "".join(content_parts) if content_parts else None
        content = _strip_thinking_content(raw_content)

        tool_calls_list: List[ToolCall] = []
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            if tc["id"] and tc["name"]:
                try:
                    args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls_list.append(
                    ToolCall(id=tc["id"], name=tc["name"], arguments=args)
                )

        usage = TokenUsage.from_usage(last_usage) if last_usage else None

        return LLMResponse(
            content=content,
            tool_calls=tool_calls_list,
            finish_reason=finish_reason,
            raw_response=None,
            usage=usage,
        )

    @staticmethod
    def _filter_thinking_delta(chunk: str, state: Dict[str, str]) -> str:
        """
        流式输出时剔除 <think>...</think> 段，仅返回可展示文本。
        """
        start_tag = "<think>"
        end_tag = "</think>"
        text = state.get("pending", "") + chunk
        mode = state.get("mode", "normal")
        out_parts: List[str] = []

        def _max_prefix_suffix_len(s: str, token: str) -> int:
            max_len = min(len(s), len(token) - 1)
            for n in range(max_len, 0, -1):
                if s.endswith(token[:n]):
                    return n
            return 0

        while text:
            if mode == "normal":
                idx = text.find(start_tag)
                if idx == -1:
                    keep = _max_prefix_suffix_len(text, start_tag)
                    if keep:
                        out_parts.append(text[:-keep])
                        text = text[-keep:]
                    else:
                        out_parts.append(text)
                        text = ""
                    break
                out_parts.append(text[:idx])
                text = text[idx + len(start_tag) :]
                mode = "in_think"
                continue

            idx = text.find(end_tag)
            if idx == -1:
                keep = _max_prefix_suffix_len(text, end_tag)
                text = text[-keep:] if keep else ""
                break
            text = text[idx + len(end_tag) :]
            mode = "normal"

        state["mode"] = mode
        state["pending"] = text
        return "".join(out_parts)

    def _build_qwen_extra_body(self) -> Optional[Dict[str, Any]]:
        """
        构建阿里云百炼 Qwen 的 extra_body 扩展参数。
        - enable_thinking 必须通过 extra_body 传递（OpenAI Python SDK）
        - thinking_budget 仅部分模型支持
        """
        if self._config.llm.provider != "qwen":
            return None

        extra_body: Dict[str, Any] = {}

        if self._config.llm.enable_search:
            extra_body["enable_search"] = True

        if self._config.llm.enable_thinking:
            extra_body["enable_thinking"] = True

        if self._config.llm.thinking_budget is not None:
            extra_body["thinking_budget"] = self._config.llm.thinking_budget

        # 搜索选项仅在 enable_search=true 时生效
        if self._config.llm.enable_search and self._config.llm.search_options:
            search_opts = self._config.llm.search_options
            search_options: Dict[str, Any] = {}

            if search_opts.forced_search:
                search_options["forced_search"] = True

            # 注意：search_strategy: agent/agent_max 会与工具冲突，工具内部单独处理
            if (
                search_opts.search_strategy not in ("agent_max", "agent")
                and search_opts.search_strategy != "turbo"
            ):
                search_options["search_strategy"] = search_opts.search_strategy

            if search_opts.enable_source:
                search_options["enable_source"] = True

            if search_opts.enable_citation and search_opts.enable_source:
                search_options["enable_citation"] = True
                if search_opts.citation_format != "[<number>]":
                    search_options["citation_format"] = search_opts.citation_format

            if search_opts.enable_search_extension:
                search_options["enable_search_extension"] = True

            if search_opts.freshness is not None:
                search_options["freshness"] = search_opts.freshness

            if search_opts.assigned_site_list:
                search_options["assigned_site_list"] = search_opts.assigned_site_list

            if search_options:
                extra_body["search_options"] = search_options

        return extra_body or None

    async def close(self) -> None:
        """关闭客户端连接"""
        await self._client.close()
