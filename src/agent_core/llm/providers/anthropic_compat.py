"""
Anthropic-compatible Messages API provider.

对接所有兼容 Anthropic Messages API 的端点：官方 Anthropic (Claude)、
Kimi Code (api.kimi.com/coding/v1) 等。

参考：https://docs.anthropic.com/en/api/messages
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
from typing import Any, Callable, Dict, List, Optional

import httpx

from agent_core.llm.capabilities import Capabilities
from agent_core.llm.response import LLMResponse, ToolCall, TokenUsage
from agent_core.llm.providers.openai_compat import _strip_thinking_content

from .base import BaseProvider

logger = logging.getLogger(__name__)


def _is_single_tool_result_user_message(msg: Dict[str, Any]) -> bool:
    if msg.get("role") != "user":
        return False
    c = msg.get("content")
    if not isinstance(c, list) or len(c) != 1:
        return False
    b0 = c[0]
    return isinstance(b0, dict) and b0.get("type") == "tool_result"


def _merge_adjacent_tool_result_user_messages(
    anthropic_messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    将连续多条「仅含一个 tool_result 块的 user」合并为一条 user，内含多个 tool_result。

    Anthropic Messages：assistant 在一轮中返回多个 tool_use 时，后续应使用**一条** user
    消息，其 content 为多个 tool_result 块；拆成多条 user 会导致部分兼容端点 400。
    """
    out: List[Dict[str, Any]] = []
    i = 0
    n = len(anthropic_messages)
    while i < n:
        m = anthropic_messages[i]
        if _is_single_tool_result_user_message(m):
            blocks: List[Dict[str, Any]] = [m["content"][0]]
            j = i + 1
            while j < n and _is_single_tool_result_user_message(anthropic_messages[j]):
                blocks.append(anthropic_messages[j]["content"][0])
                j += 1
            out.append({"role": "user", "content": blocks})
            i = j
        else:
            out.append(m)
            i += 1
    return out


def _tool_use_input_from_openai_tc(tc: Dict[str, Any]) -> Dict[str, Any]:
    """从 OpenAI 格式 tool_call 解析 Anthropic ``tool_use.input`` 对象。"""
    fn = tc.get("function")
    raw: Any
    if isinstance(fn, dict):
        raw = fn.get("arguments", {})
    else:
        raw = tc.get("arguments", {})
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _tool_name_from_openai_tc(tc: Dict[str, Any]) -> str:
    fn = tc.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn.get("name", ""))
    name = tc.get("name")
    return str(name) if name else ""


class AnthropicCompatProvider(BaseProvider):
    """对接 Anthropic 兼容端点的 provider。"""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        capabilities: Capabilities,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        request_timeout_seconds: float = 120.0,
        stream: bool = False,
        vendor_params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self._name = name
        self._model = model
        self._capabilities = capabilities
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._stream = stream
        self._vendor_params = dict(vendor_params or {})

        # 构建 HTTP 客户端
        http_headers = dict(headers or {})
        # Anthropic 需要特定 header
        http_headers.setdefault("anthropic-version", "2023-06-01")
        
        self._http_client = httpx.AsyncClient(
            headers=http_headers,
            timeout=request_timeout_seconds,
        )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    @property
    def context_window(self) -> int:
        cw = self._capabilities.context_window
        if cw is not None:
            return cw
        # 根据模型名启发式估算
        m = self._model.lower()
        if "claude-3" in m or "kimi" in m:
            return 200_000
        return 100_000

    @property
    def temperature(self) -> float:
        return self._temperature

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def _build_auth_headers(self) -> Dict[str, str]:
        """构建认证请求头。"""
        headers = {"Authorization": f"Bearer {self._api_key}"}
        headers.update(self._http_client.headers)
        return headers

    async def _make_request(
        self,
        endpoint: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """发送 POST 请求并返回解析后的 JSON 响应。"""
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        headers = self._build_auth_headers()
        headers["Content-Type"] = "application/json"

        # 合并 vendor_params 到 payload
        if self._vendor_params:
            payload.update(self._vendor_params)

        response = await self._http_client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

    def _convert_messages(
        self,
        messages: List[Dict[str, Any]],
        system_message: Optional[str] = None,
    ) -> tuple[Optional[str], List[Dict[str, Any]]]:
        """
        将 OpenAI 格式消息转换为 Anthropic 格式。
        
        Anthropic 格式：
        - system 作为单独参数
        - messages 只包含 user/assistant 角色
        - 不支持 system role 在 messages 数组中
        
        带 tool_calls 的 assistant 之后，必须在**一条** user 消息里包含与每个 tool_use_id
        对应的 tool_result。若 OpenAI 历史里在两条 role=tool 之间插入了普通 user（或顺序
        不连续），仅靠「相邻合并」会漏掉第二个 tool_result，导致 Kimi/Anthropic 400。
        因此对每条带 tool_calls 的 assistant，会向前扫描到下一条 assistant 为止，按
        tool_calls 顺序收集所有匹配的 role=tool。
        
        返回：(system_content, anthropic_messages)
        """
        anthropic_messages: List[Dict[str, Any]] = []
        n = len(messages)
        consumed = [False] * n
        i = 0

        while i < n:
            if consumed[i]:
                i += 1
                continue

            msg = messages[i]
            role = msg.get("role")
            content = msg.get("content")

            if role == "system":
                if system_message:
                    system_message = f"{system_message}\n{content}"
                else:
                    system_message = content
                i += 1
                continue

            if role == "assistant":
                amc = msg.get("anthropic_message_content")
                if isinstance(amc, list) and len(amc) > 0:
                    anthropic_messages.append({"role": "assistant", "content": amc})
                    ids_order = [
                        str(b.get("id", "") or "")
                        for b in amc
                        if isinstance(b, dict) and b.get("type") == "tool_use"
                    ]
                    if ids_order:
                        expected = set(ids_order)
                        tool_body: Dict[str, Any] = {}
                        j = i + 1
                        while j < n and messages[j].get("role") != "assistant":
                            if messages[j].get("role") == "tool":
                                tid = str(messages[j].get("tool_call_id", "") or "")
                                if tid in expected:
                                    tool_body[tid] = messages[j].get("content", "")
                                    consumed[j] = True
                            j += 1
                        blocks: List[Dict[str, Any]] = []
                        for tid in ids_order:
                            if tid in tool_body:
                                blocks.append({
                                    "type": "tool_result",
                                    "tool_use_id": tid,
                                    "content": tool_body[tid],
                                })
                        if blocks:
                            anthropic_messages.append({"role": "user", "content": blocks})
                    i += 1
                    continue

                tool_calls = msg.get("tool_calls") or []
                if tool_calls:
                    content_parts: List[Dict[str, Any]] = []
                    if content:
                        content_parts.append({"type": "text", "text": content})
                    ids_order = []
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        tid = str(tc.get("id", "") or "")
                        ids_order.append(tid)
                        content_parts.append({
                            "type": "tool_use",
                            "id": tid,
                            "name": _tool_name_from_openai_tc(tc),
                            "input": _tool_use_input_from_openai_tc(tc),
                        })
                    anthropic_messages.append({"role": "assistant", "content": content_parts})

                    expected = set(ids_order)
                    tool_body = {}
                    j = i + 1
                    while j < n and messages[j].get("role") != "assistant":
                        if messages[j].get("role") == "tool":
                            tid = str(messages[j].get("tool_call_id", "") or "")
                            if tid in expected:
                                tool_body[tid] = messages[j].get("content", "")
                                consumed[j] = True
                        j += 1
                    blocks = []
                    for tid in ids_order:
                        if tid in tool_body:
                            blocks.append({
                                "type": "tool_result",
                                "tool_use_id": tid,
                                "content": tool_body[tid],
                            })
                    if blocks:
                        anthropic_messages.append({"role": "user", "content": blocks})
                else:
                    anthropic_messages.append({"role": "assistant", "content": content or ""})
                i += 1
                continue

            if role == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                content_text = msg.get("content", "")
                anthropic_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_call_id,
                            "content": content_text,
                        }
                    ],
                })
                i += 1
                continue

            if role == "user":
                anthropic_messages.append({"role": "user", "content": content})
                i += 1
                continue

            i += 1

        anthropic_messages = _merge_adjacent_tool_result_user_messages(
            anthropic_messages
        )

        return system_message, anthropic_messages

    def _convert_tools(
        self,
        tools: Optional[List[Dict[str, Any]]],
    ) -> Optional[List[Dict[str, Any]]]:
        """
        将 OpenAI 格式工具定义转换为 Anthropic 格式。
        
        OpenAI 格式：
        {
            "type": "function",
            "function": {
                "name": "...",
                "description": "...",
                "parameters": {...}
            }
        }
        
        Anthropic 格式：
        {
            "name": "...",
            "description": "...",
            "input_schema": {...}
        }
        """
        if not tools:
            return None
        
        anthropic_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                anthropic_tools.append({
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                })
        return anthropic_tools

    def _parse_response(self, response_data: Dict[str, Any]) -> LLMResponse:
        """解析 Anthropic API 响应为 LLMResponse。

        扩展思考（extended thinking）：``content`` 中先有 ``thinking`` 块，再有 ``text`` 块，
        见 https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking

        部分兼容端点会把可读思考放在 ``type: \"text\"`` 的前若干块里、最后一块才是对用户回复；
        当 ``capabilities.reasoning_content`` 为真且存在多个 ``text`` 块、又没有规范的
        ``thinking`` 块时，将除最后一块外的 ``text`` 视为推理文本，仅最后一块作为 ``content``。
        """
        content: Optional[str] = None
        tool_calls: List[ToolCall] = []
        finish_reason = response_data.get("stop_reason", "stop")

        content_items = response_data.get("content", [])
        text_parts: List[str] = []
        thinking_parts: List[str] = []

        for item in content_items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "thinking":
                t = item.get("thinking")
                if isinstance(t, str) and t.strip():
                    thinking_parts.append(t.strip())
            elif item_type == "text":
                t = item.get("text")
                if isinstance(t, str):
                    text_parts.append(t)
            elif item_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=item.get("id", ""),
                        name=item.get("name", ""),
                        arguments=item.get("input", {}),
                    )
                )
            # redacted_thinking 等块不参与展示性 content，多轮需原样回传时由调用方用 raw_response 处理

        reasoning_content: Optional[str] = None
        if thinking_parts:
            reasoning_content = "\n\n".join(thinking_parts).strip()

        if text_parts:
            if reasoning_content:
                # 规范扩展思考：对用户可见部分仅为 text 块
                content = "\n".join(text_parts).strip()
            elif (
                self._capabilities.reasoning_content
                and len(text_parts) > 1
            ):
                # 兼容：多个 text 块且无端点返回的 thinking 块时，假定最后一块为最终回复
                reasoning_content = "\n\n".join(s.strip() for s in text_parts[:-1] if s.strip()).strip()
                content = (text_parts[-1] or "").strip()
            else:
                content = "\n".join(text_parts).strip()

        if content and self._capabilities.thinking_tag_inline:
            content = _strip_thinking_content(content)
        if content == "":
            content = None

        if reasoning_content == "":
            reasoning_content = None

        usage_data = response_data.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=usage_data.get("input_tokens", 0),
            completion_tokens=usage_data.get("output_tokens", 0),
            total_tokens=usage_data.get("input_tokens", 0) + usage_data.get("output_tokens", 0),
        )

        raw_blocks = response_data.get("content", [])
        anthropic_message_content: Optional[List[Dict[str, Any]]] = None
        if isinstance(raw_blocks, list) and raw_blocks:
            anthropic_message_content = copy.deepcopy(raw_blocks)

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            raw_response=response_data,
            usage=usage,
            reasoning_content=reasoning_content,
            anthropic_message_content=anthropic_message_content,
        )

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        system_message: Optional[str] = None,
    ) -> LLMResponse:
        """不带工具的基础对话。"""
        system, anthropic_messages = self._convert_messages(messages, system_message)
        
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": anthropic_messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        
        if system:
            payload["system"] = system
        
        response_data = await self._make_request("/messages", payload)
        return self._parse_response(response_data)

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
        """支持工具调用的对话。"""
        system, anthropic_messages = self._convert_messages(messages, system_message)
        anthropic_tools = self._convert_tools(tools)

        effective_max_tokens = (
            int(max_tokens_override)
            if max_tokens_override and max_tokens_override > 0
            else self._max_tokens
        )
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": anthropic_messages,
            "max_tokens": effective_max_tokens,
            "temperature": self._temperature,
        }
        
        if system:
            payload["system"] = system
        
        if anthropic_tools and self._capabilities.function_calling:
            payload["tools"] = anthropic_tools
            # Anthropic 的 tool_choice 格式
            if tool_choice == "auto":
                payload["tool_choice"] = {"type": "auto"}
            elif tool_choice == "required":
                payload["tool_choice"] = {"type": "any"}
            elif tool_choice == "none":
                payload["tool_choice"] = {"type": "none"}
            else:
                # 指定具体工具
                payload["tool_choice"] = {"type": "tool", "name": tool_choice}
        
        response_data = await self._make_request("/messages", payload)
        return self._parse_response(response_data)

    async def chat_with_image(
        self,
        prompt: str,
        image_url: str,
        system_message: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> LLMResponse:
        """多模态识图。"""
        # Anthropic 支持 base64 图片
        # 注意：这里简化处理，假设 image_url 是 http(s) URL
        # 实际使用中可能需要先下载图片转为 base64
        
        # 对于 Kimi Code 等支持 URL 的端点，直接传 URL
        # 对于需要 base64 的端点，需要先下载转换
        # 这里采用通用方案：传 URL，让后端处理
        
        system, _ = self._convert_messages([], system_message)
        
        anthropic_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image", "source": {"type": "url", "url": image_url}},
                ],
            }
        ]
        
        payload: Dict[str, Any] = {
            "model": model_override or self._model,
            "messages": anthropic_messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        
        if system:
            payload["system"] = system
        
        response_data = await self._make_request("/messages", payload)
        return self._parse_response(response_data)

    async def close(self) -> None:
        """释放底层 HTTP 连接。"""
        await self._http_client.aclose()
