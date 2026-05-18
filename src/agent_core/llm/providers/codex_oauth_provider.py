"""
OpenAI Codex OAuth Provider — ChatGPT Plus/Team/Pro 订阅。

通过 Codex Responses API 调用模型，支持 SSE 流式响应。
API 端点：https://chatgpt.com/backend-api/codex/responses

特性：
- 自动 token 刷新
- SSE 流解析（文本 + 工具调用）
- GPT-5.5 推理档位（none/low/medium/high/xhigh）
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from agent_core.llm.capabilities import Capabilities
from agent_core.llm.response import LLMResponse, ToolCall, TokenUsage
from agent_core.llm.providers.base import BaseProvider
from system.oauth.codex_oauth import (
    load_token_state,
    refresh_access_token,
    save_token_state,
    TokenState,
)
from litellm.completion_extras.litellm_responses_transformation.transformation import (
    LiteLLMResponsesTransformationHandler,
)

logger = logging.getLogger(__name__)

# ── LiteLLM 桥接（Responses API ⇄ Chat Completions 转换） ──
_litellm_handler = LiteLLMResponsesTransformationHandler()


def _convert_tools(
    tools: Optional[List[Dict[str, Any]]],
) -> Optional[List[Dict[str, Any]]]:
    """Chat Completions tool 格式 → Responses API tool 格式（由 LiteLLM 处理）。"""
    if not tools:
        return None
    return _litellm_handler._convert_tools_to_responses_format(tools)


def _convert_messages(
    messages: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Chat Completions messages → Responses API input（由 LiteLLM 处理）。

    Returns:
        (input_items, instructions)
        其中 system 消息被提取为 instructions。
    """
    input_items, instructions = _litellm_handler.convert_chat_completion_messages_to_responses_api(messages)

    # 回放上一轮保存的 Responses reasoning item（含 encrypted_content），
    # 便于 store=false 场景下维持多轮推理态。
    replay_items: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        raw = msg.get("responses_reasoning_items")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "reasoning":
                continue
            sanitized: Dict[str, Any] = {"type": "reasoning"}
            encrypted = item.get("encrypted_content")
            summary = item.get("summary")
            if isinstance(encrypted, str) and encrypted:
                sanitized["encrypted_content"] = encrypted
            if isinstance(summary, list):
                sanitized["summary"] = summary
            if "encrypted_content" in sanitized or "summary" in sanitized:
                replay_items.append(sanitized)

    if replay_items:
        input_items = replay_items + list(input_items)

    return input_items, instructions


DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex/responses"
REFRESH_MARGIN_SECONDS = 60

# 推理档位
REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh")

_SSE_LINE_RE = re.compile(r"^(event|data):\s?(.*)")


def _new_tool_call_entry() -> Dict[str, Any]:
    return {"id": "", "item_id": "", "name": "", "arguments": ""}


def _tool_call_key(
    data: Dict[str, Any],
    item: Optional[Dict[str, Any]] = None,
) -> tuple[str, Any]:
    idx = data.get("output_index")
    if idx is not None:
        try:
            return ("idx", int(idx))
        except (TypeError, ValueError):
            return ("idx", str(idx))

    src = item if isinstance(item, dict) else data
    for field in ("call_id", "item_id", "id"):
        val = src.get(field) if isinstance(src, dict) else None
        if val:
            return (field, str(val))
    return ("idx", 0)


def _merge_function_call_item(
    tool_calls_map: Dict[tuple[str, Any], Dict[str, Any]],
    key: tuple[str, Any],
    item: Dict[str, Any],
    *,
    overwrite_arguments: bool = False,
) -> None:
    entry = tool_calls_map.setdefault(key, _new_tool_call_entry())
    call_id = item.get("call_id")
    item_id = item.get("id") or item.get("item_id")
    name = item.get("name")
    arguments = item.get("arguments")

    # Responses API has both an output item id and a function call_id. The
    # follow-up function_call_output must use call_id, so prefer it.
    if call_id:
        entry["id"] = str(call_id)
    elif item_id and not entry["id"]:
        entry["id"] = str(item_id)
    if item_id:
        entry["item_id"] = str(item_id)
    if name:
        entry["name"] = str(name)
    if isinstance(arguments, str) and (
        overwrite_arguments or arguments or not entry["arguments"]
    ):
        entry["arguments"] = arguments


def _extract_output_text_from_item(item: Dict[str, Any]) -> str:
    if item.get("type") != "message":
        return ""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for content_item in content:
        if not isinstance(content_item, dict):
            continue
        if content_item.get("type") in {"output_text", "text"}:
            text = content_item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


async def _maybe_emit_delta(
    on_delta: Optional[Callable[[str], Any]],
    delta: str,
) -> None:
    if not on_delta or not delta:
        return
    maybe_awaitable = on_delta(delta)
    if inspect.isawaitable(maybe_awaitable):
        await maybe_awaitable


async def _parse_sse_stream(
    response: httpx.Response,
    on_delta: Optional[Callable[[str], Any]] = None,
    on_reasoning_delta: Optional[Callable[[str], Any]] = None,
) -> tuple[LLMResponse, Optional[str]]:
    """解析 Codex Responses SSE 流。

    提取：文本增量、工具调用参数 delta、最终 usage、response_id。

    Returns:
        (LLMResponse, response_id)
    """
    text_parts: List[str] = []
    completed_text_parts: List[str] = []
    reasoning_text_parts: List[str] = []
    responses_reasoning_items: List[Dict[str, Any]] = []
    tool_calls_map: Dict[tuple[str, Any], Dict[str, Any]] = {}
    usage: Optional[TokenUsage] = None
    response_id: Optional[str] = None
    finish_reason = "stop"

    async def handle_sse_block(block: str) -> None:
        nonlocal response_id, usage, finish_reason

        if not block.strip():
            return

        data_lines: List[str] = []
        event_type: Optional[str] = None
        for line in block.splitlines():
            line = line.rstrip("\r")
            if not line or line.startswith(":"):
                continue
            m = _SSE_LINE_RE.match(line)
            if not m:
                continue
            field, value = m.group(1), m.group(2)
            if field == "event":
                event_type = value
            elif field == "data":
                data_lines.append(value)

        if not data_lines:
            return
        data_str = "\n".join(data_lines).strip()
        if not data_str or data_str == "[DONE]":
            return

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            logger.debug("忽略无法解析的 Codex SSE data: %r", data_str[:300])
            return

        evt_type = data.get("type", event_type or "")

        def _capture_reasoning_item(item: Dict[str, Any]) -> None:
            if item.get("type") != "reasoning":
                return
            summary = item.get("summary")
            encrypted = item.get("encrypted_content")
            saved: Dict[str, Any] = {"type": "reasoning"}
            if isinstance(summary, list):
                saved["summary"] = summary
                for s in summary:
                    txt = s.get("text") if isinstance(s, dict) else None
                    if isinstance(txt, str) and txt.strip():
                        reasoning_text_parts.append(txt.strip())
            if isinstance(encrypted, str) and encrypted:
                saved["encrypted_content"] = encrypted
            if "summary" in saved or "encrypted_content" in saved:
                responses_reasoning_items.append(saved)

        # 提取 response_id（用于多轮对话的 previous_response_id）
        if evt_type == "response.created":
            response_id = data.get("response", {}).get("id")

        # 文本增量
        elif evt_type == "response.output_text.delta":
            delta = data.get("delta", "")
            if delta:
                text_parts.append(delta)
                await _maybe_emit_delta(on_delta, delta)

        # 文本完成事件：只作为无 delta 时的兜底，避免重复拼接。
        elif evt_type == "response.output_text.done":
            text = data.get("text", "")
            if isinstance(text, str) and text:
                completed_text_parts.append(text)

        # 工具调用 output item 包含 name/call_id；arguments delta 事件通常不带 name。
        elif evt_type in ("response.output_item.added", "response.output_item.done"):
            item = data.get("item")
            if isinstance(item, dict):
                if item.get("type") == "function_call":
                    _merge_function_call_item(
                        tool_calls_map,
                        _tool_call_key(data, item),
                        item,
                        overwrite_arguments=evt_type == "response.output_item.done",
                    )
                else:
                    _capture_reasoning_item(item)
                    text = _extract_output_text_from_item(item)
                    if text:
                        completed_text_parts.append(text)

        # 工具调用参数增量
        elif evt_type == "response.function_call_arguments.delta":
            key = _tool_call_key(data)
            entry = tool_calls_map.setdefault(key, _new_tool_call_entry())
            delta = data.get("delta", "")
            item_id = data.get("item_id", "")
            if item_id:
                entry["item_id"] = str(item_id)
                if not entry["id"]:
                    entry["id"] = str(item_id)
            if isinstance(delta, str):
                entry["arguments"] += delta

        # 工具调用完成
        elif evt_type == "response.function_call_arguments.done":
            key = _tool_call_key(data)
            entry = tool_calls_map.setdefault(key, _new_tool_call_entry())
            item_id = data.get("item_id", "")
            call_id = data.get("call_id", "")
            name = data.get("name", "")
            full_args = data.get("arguments", "")
            if call_id:
                entry["id"] = str(call_id)
            elif item_id and not entry["id"]:
                entry["id"] = str(item_id)
            if item_id:
                entry["item_id"] = str(item_id)
            if name:
                entry["name"] = str(name)
            if isinstance(full_args, str):
                entry["arguments"] = full_args

        # 完成事件 → 提取 usage；同时用最终 output 兜底补齐 tool calls/text。
        elif evt_type == "response.completed":
            response_payload = data.get("response", {})
            if isinstance(response_payload, dict):
                response_id = response_payload.get("id") or response_id
                usage_data = response_payload.get("usage")
                if usage_data:
                    usage = TokenUsage(
                        prompt_tokens=usage_data.get("input_tokens", 0),
                        completion_tokens=usage_data.get("output_tokens", 0),
                        total_tokens=usage_data.get("total_tokens", 0),
                    )

                output = response_payload.get("output")
                if isinstance(output, list):
                    completed_output_texts: List[str] = []
                    for idx, item in enumerate(output):
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "function_call":
                            _merge_function_call_item(
                                tool_calls_map,
                                ("idx", idx),
                                item,
                                overwrite_arguments=True,
                            )
                        else:
                            _capture_reasoning_item(item)
                            text = _extract_output_text_from_item(item)
                            if text:
                                completed_output_texts.append(text)
                    if completed_output_texts:
                        completed_text_parts[:] = completed_output_texts

        # 错误事件
        elif evt_type in ("response.failed", "error"):
            error = data.get("error", {})
            message = (
                error.get("message", json.dumps(data))
                if isinstance(error, dict)
                else json.dumps(data)
            )
            raise _CodexAPIError(
                message,
                status_code=getattr(response, "status_code", 502),
            )

    buffer = ""
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        try:
            buffer += chunk.decode("utf-8", errors="replace")
        except Exception:
            continue
        buffer = buffer.replace("\r\n", "\n")

        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            await handle_sse_block(block)

    if buffer.strip():
        await handle_sse_block(buffer)

    # 构建 tool_calls 列表
    tool_calls: List[ToolCall] = []
    tool_call_positions: Dict[str, int] = {}
    sorted_keys = sorted(tool_calls_map.keys(), key=lambda k: (k[0], str(k[1])))
    for out_idx, key in enumerate(sorted_keys):
        tc = tool_calls_map[key]
        if tc["name"]:
            raw_args = tc["arguments"] or ""
            try:
                args = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError):
                args = raw_args
            call_id = tc["id"] or f"call_{out_idx}"
            tool_call = ToolCall(
                id=call_id,
                name=tc["name"],
                arguments=args,
            )
            existing_idx = tool_call_positions.get(call_id)
            if existing_idx is None:
                tool_call_positions[call_id] = len(tool_calls)
                tool_calls.append(tool_call)
            elif tool_calls[existing_idx].arguments in ({}, "") and args not in (
                {},
                "",
            ):
                tool_calls[existing_idx] = tool_call
        else:
            logger.warning(
                "Codex SSE tool call missing name; dropped item_id=%s id=%s args_len=%d",
                tc.get("item_id"),
                tc.get("id"),
                len(tc.get("arguments") or ""),
            )

    content = "".join(text_parts).strip()
    if not content:
        content = "".join(completed_text_parts).strip()
    reasoning_content = "\n\n".join(x for x in reasoning_text_parts if x).strip() or None
    if reasoning_content:
        await _maybe_emit_delta(on_reasoning_delta, reasoning_content)

    return (
        LLMResponse(
            content=content or None,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason="tool_calls" if tool_calls else finish_reason,
            raw_response=None,
            reasoning_content=reasoning_content,
            responses_reasoning_items=responses_reasoning_items or None,
        ),
        response_id,
    )


class _CodexAPIError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _is_401(exc: Exception) -> bool:
    status = None
    if hasattr(exc, "response"):
        resp = getattr(exc, "response", None)
        if resp is not None and hasattr(resp, "status_code"):
            status = resp.status_code
    if status is None:
        status = getattr(exc, "status_code", None)
    return status == 401


class CodexOAuthProvider(BaseProvider):
    """基于 Codex Responses API 的 provider。

    配置示例（config/llm/providers.d/codex_oauth.yaml）：
        gpt-5.5:
          protocol: "codex_oauth"
          base_url: "https://chatgpt.com/backend-api/codex/responses"
          model: "gpt-5.5"
          auth_file: "./data/oauth/chatgpt-plus.json"
          reasoning_effort: "medium"   # none|low|medium|high|xhigh
    """

    def __init__(
        self,
        *,
        name: str,
        auth_file: str,
        model: str,
        capabilities: Capabilities,
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        request_timeout_seconds: float = 300.0,
        stream: bool = True,
        vendor_params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        reasoning_effort: str = "medium",
    ) -> None:
        self._name = name
        self._auth_file = Path(auth_file)
        self._model_name = model
        self._capabilities = capabilities
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._stream = stream
        self._vendor_params = dict(vendor_params or {})
        self._headers = dict(headers or {})
        self._base_url = base_url
        self._request_timeout_seconds = request_timeout_seconds
        self._reasoning_effort = (
            reasoning_effort if reasoning_effort in REASONING_EFFORTS else "medium"
        )

        self._token_state: Optional[TokenState] = load_token_state(self._auth_file)
        self._refresh_lock = asyncio.Lock()
        self._previous_response_id: Optional[str] = None

        if self._token_state is None:
            logger.warning(
                "CodexOAuthProvider(%s): token 文件 %s 不存在，请先运行 oauth login",
                name,
                auth_file,
            )

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    @property
    def context_window(self) -> int:
        cw = self._capabilities.context_window
        return cw if cw is not None else 1_000_000  # GPT-5.5 = 1M

    @property
    def temperature(self) -> float:
        return self._temperature

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    # ── Token 管理 ──

    async def _ensure_fresh_token(self) -> str:
        async with self._refresh_lock:
            if self._token_state is None:
                raise RuntimeError(
                    f"CodexOAuthProvider({self._name}): 未找到 token，请先运行 oauth login"
                )
            now = time.time()
            if self._token_state.expires_at - now > REFRESH_MARGIN_SECONDS:
                return self._token_state.access_token

            logger.info("刷新 Codex token...")
            data = await refresh_access_token(self._token_state.refresh_token)
            new_state = TokenState(
                access_token=data["access_token"],
                refresh_token=data.get(
                    "refresh_token", self._token_state.refresh_token
                ),
                expires_at=now + data.get("expires_in", 86400),
            )
            save_token_state(self._auth_file, new_state)
            self._token_state = new_state
            return new_state.access_token

    # ── 请求 ──

    async def _make_request(
        self,
        instructions: str,
        input_list: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        on_delta: Optional[Callable[[str], Any]] = None,
        on_reasoning_delta: Optional[Callable[[str], Any]] = None,
    ) -> LLMResponse:
        access_token = await self._ensure_fresh_token()

        # 空 input 防护：Responses API 不允许空 input
        if not input_list:
            input_list = [{"role": "user", "content": "."}]

        body: Dict[str, Any] = {
            "model": self._model_name,
            "instructions": instructions,
            "input": input_list,
            "store": False,
            "stream": True,
            "reasoning": {"effort": self._reasoning_effort, "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
        }

        # 多轮优化：该端点不支持 previous_response_id（会报 Unsupported parameter），
        # 因此不传此参数，靠完整的 input 数组维护对话历史。
        # if self._previous_response_id:
        #     body["previous_response_id"] = self._previous_response_id

        resp_tools = _convert_tools(tools)
        if resp_tools:
            body["tools"] = resp_tools
            body["tool_choice"] = "auto"

        for key, val in self._vendor_params.items():
            if key not in body:
                body[key] = val

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Origin": "https://chatgpt.com",
        }
        headers.update(self._headers)

        last_connect_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                async with httpx.AsyncClient(
                    timeout=self._request_timeout_seconds
                ) as client:
                    async with client.stream(
                        "POST",
                        self._base_url,
                        json=body,
                        headers=headers,
                    ) as response:
                        if response.status_code == 401:
                            raise _CodexAPIError("Unauthorized", 401)
                        if response.status_code != 200:
                            error_bytes = await response.aread()
                            error_text = error_bytes.decode("utf-8", errors="replace")
                            raise _CodexAPIError(
                                f"Codex API error ({response.status_code}): {error_text[:500]}",
                                response.status_code,
                            )

                        result, response_id = await _parse_sse_stream(
                            response,
                            on_delta=on_delta,
                            on_reasoning_delta=on_reasoning_delta,
                        )

                        # 该端点不支持 previous_response_id，因此不再保存 response_id。
                        # if response_id:
                        #     self._previous_response_id = response_id

                        return result
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError) as exc:
                last_connect_error = exc
                if attempt >= 3:
                    break
                logger.warning(
                    "Codex stream connect failed (attempt %d/3), retrying: %s",
                    attempt,
                    exc,
                )
                await asyncio.sleep(0.8 * attempt)

        if last_connect_error is not None:
            raise last_connect_error
        raise RuntimeError("Codex stream request failed without response")

    # ── 公开接口 ──

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        system_message: Optional[str] = None,
    ) -> LLMResponse:
        return await self._do_chat(messages, system_message, None, None)

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
        # 该端点不支持 previous_response_id，因此不传此参数。
        # 多轮对话状态通过完整的 input 数组维护。
        return await self._do_chat(
            messages,
            system_message,
            tools,
            on_content_delta,
            on_reasoning_delta,
        )

    async def _do_chat(
        self,
        messages: List[Dict[str, Any]],
        system_message: Optional[str],
        tools: Optional[List[Dict[str, Any]]],
        on_delta: Optional[Callable[[str], Any]],
        on_reasoning_delta: Optional[Callable[[str], Any]] = None,
    ) -> LLMResponse:
        input_list, litellm_instructions = _convert_messages(messages)
        if system_message and litellm_instructions:
            instructions = f"{system_message}\n{litellm_instructions}"
        else:
            instructions = (
                system_message or litellm_instructions or "You are a helpful assistant."
            )

        try:
            return await self._make_request(
                instructions,
                input_list,
                tools,
                on_delta,
                on_reasoning_delta=on_reasoning_delta,
            )
        except _CodexAPIError as e:
            if e.status_code == 401:
                logger.info("401，刷新 token 后重试")
                async with self._refresh_lock:
                    self._token_state = None
                return await self._make_request(
                    instructions,
                    input_list,
                    tools,
                    on_delta,
                    on_reasoning_delta=on_reasoning_delta,
                )
            raise
        except Exception as e:
            if _is_401(e):
                logger.info("401（httpx），刷新后重试")
                async with self._refresh_lock:
                    self._token_state = None
                return await self._make_request(
                    instructions,
                    input_list,
                    tools,
                    on_delta,
                    on_reasoning_delta=on_reasoning_delta,
                )
            raise

    async def chat_with_image(
        self,
        prompt: str,
        image_url: str,
        system_message: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> LLMResponse:
        input_list = [
            {"role": "user", "content": f"{prompt}\n[Image URL: {image_url}]"}
        ]
        instructions = system_message or "You are a helpful assistant."
        return await self._make_request(instructions, input_list)

    async def close(self) -> None:
        pass
