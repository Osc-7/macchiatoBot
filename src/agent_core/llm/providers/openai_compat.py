"""
OpenAI-compatible Chat Completions provider.

对接所有兼容 OpenAI Chat Completions API 的端点：官方 OpenAI、Azure OpenAI、
本地 vLLM、OpenRouter、阿里云百炼兼容模式、豆包、上海交大 models.sjtu.edu.cn 等。

vendor_params 原样作为 SDK 的 extra_body 下发。
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import uuid
from typing import Any, Callable, Dict, List, Optional

import httpx
from openai import AsyncOpenAI  # type: ignore

from agent_core.llm.capabilities import Capabilities
from agent_core.llm.response import LLMResponse, ToolCall, TokenUsage

from .base import BaseProvider

logger = logging.getLogger(__name__)

# Gemini 多轮工具：无 API 生成签名时（跨模型迁移、中途切换、客户端合成 tool_calls）须带占位符，否则 400。
# 官方允许二选一，见 https://ai.google.dev/gemini-api/docs/thought-signatures
_GEMINI_DUMMY_THOUGHT_SIGNATURE_SKIP = "skip_thought_signature_validator"


def _tool_call_extra_content(tc: Any) -> Optional[Dict[str, Any]]:
    """解析 OpenAI 兼容响应里单个 tool_call 上的 ``extra_content``（Gemini/Kimi 等附带的 thought_signature 等）。"""
    ex = getattr(tc, "extra_content", None)
    if isinstance(ex, dict) and ex:
        return ex
    me = getattr(tc, "model_extra", None)
    if isinstance(me, dict):
        nested = me.get("extra_content")
        if isinstance(nested, dict) and nested:
            return nested
    return None


def _gemini_tc_has_thought_signature(tc: Dict[str, Any]) -> bool:
    ex = tc.get("extra_content")
    if not isinstance(ex, dict):
        return False
    g = ex.get("google")
    if not isinstance(g, dict):
        return False
    sig = g.get("thought_signature")
    return isinstance(sig, str) and bool(sig.strip())


def inject_gemini_dummy_thought_signatures_in_messages(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    为 assistant 的 ``tool_calls`` 补齐 ``extra_content.google.thought_signature``。

    仅应在确认请求发往 Gemini OpenAI 兼容端点时使用；占位符为官方文档允许的 dummy，
    用于无真实签名的历史（见模块常量说明）。
    """
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            out.append(msg)
            continue
        tcs = msg.get("tool_calls")
        if not isinstance(tcs, list) or not tcs:
            out.append(msg)
            continue
        need_any = False
        for tc in tcs:
            if isinstance(tc, dict) and not _gemini_tc_has_thought_signature(tc):
                need_any = True
                break
        if not need_any:
            out.append(msg)
            continue
        new_tcs: List[Any] = []
        for tc in tcs:
            if not isinstance(tc, dict):
                new_tcs.append(tc)
                continue
            if _gemini_tc_has_thought_signature(tc):
                new_tcs.append(tc)
                continue
            ex = tc.get("extra_content")
            ex2 = dict(ex) if isinstance(ex, dict) else {}
            g = ex2.get("google")
            g2 = dict(g) if isinstance(g, dict) else {}
            g2["thought_signature"] = _GEMINI_DUMMY_THOUGHT_SIGNATURE_SKIP
            ex2["google"] = g2
            tc2 = dict(tc)
            tc2["extra_content"] = ex2
            new_tcs.append(tc2)
        msg2 = dict(msg)
        msg2["tool_calls"] = new_tcs
        out.append(msg2)
    return out


# Qwen 深度思考模式会将推理内容放在 content 中（有时与回复混合），用 <think>...</think> 包裹。
# 参见 https://www.alibabacloud.com/help/zh/model-studio/deep-thinking
THINKING_END_TAG = "</think>"


def _strip_thinking_content(content: Optional[str]) -> Optional[str]:
    """剥离 Qwen 等模型输出中 </think> 之前的思考内容。"""
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


_TOOL_CODE_RE = re.compile(
    r"<tool_code>\s*(\w+)\((.*?)\)\s*</tool_code>",
    re.DOTALL,
)


def _extract_tool_code_calls(
    content: Optional[str],
    existing_tool_calls: List[ToolCall],
) -> tuple[Optional[str], List[ToolCall]]:
    """
    部分模型（如 Qwen thinking mode）会把工具调用写成
    ``<tool_code>func_name(arg=val)</tool_code>`` 文本。若已有正规 tool_calls 则不处理。
    """
    if existing_tool_calls or not content:
        return content, existing_tool_calls

    matches = list(_TOOL_CODE_RE.finditer(content))
    if not matches:
        return content, existing_tool_calls

    calls: List[ToolCall] = []
    for m in matches:
        func_name = m.group(1)
        raw_args = m.group(2).strip()
        args: dict = {}
        if raw_args:
            try:
                args = json.loads("{" + raw_args + "}")
            except (json.JSONDecodeError, ValueError):
                for part in raw_args.split(","):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("\"'")
                        args[k] = v
        calls.append(
            ToolCall(
                id=f"toolcode-{uuid.uuid4().hex[:8]}",
                name=func_name,
                arguments=args,
            )
        )

    cleaned = _TOOL_CODE_RE.sub("", content).strip()
    return cleaned or None, calls


def get_context_window_tokens_for_model(model: str) -> int:
    """
    根据模型名启发式估算上下文窗口（仅作为兜底；优先使用 capabilities.context_window）。
    """
    if not model:
        return 200_000

    m = model.lower()

    if "qwen" in m and "3.5" in m:
        if "plus" in m or "1m" in m:
            return 1_000_000
        return 256_000

    if "qwen" in m and "2.5" in m and "1m" in m:
        return 1_000_000

    return 200_000


class OpenAICompatProvider(BaseProvider):
    """对接 OpenAI 兼容端点的 provider。"""

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
        self._base_url = str(base_url or "")
        self._model = model
        self._capabilities = capabilities
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._stream = stream
        self._vendor_params = dict(vendor_params or {})

        # 支持自定义 HTTP headers（如 Kimi Code 需要 User-Agent: claude-code/0.1.0）
        if headers:
            http_client = httpx.AsyncClient(headers=headers, timeout=request_timeout_seconds)
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                http_client=http_client,
            )
        else:
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=request_timeout_seconds,
            )

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
        return get_context_window_tokens_for_model(self._model)

    @property
    def temperature(self) -> float:
        return self._temperature

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def _apply_vendor_extra_body(self, request_params: Dict[str, Any]) -> None:
        if self._vendor_params:
            request_params["extra_body"] = self._vendor_params

    def _is_likely_gemini_openai_target(self) -> bool:
        """是否对请求消息做 Gemini thought_signature 占位符注入（中途切模型 / 跨模型历史）。"""
        bu = self._base_url.lower()
        if "generativelanguage.googleapis.com" in bu:
            return True
        m = (self._model or "").lower()
        return "gemini" in m

    def _prepare_openai_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self._is_likely_gemini_openai_target():
            return messages
        return inject_gemini_dummy_thought_signatures_in_messages(messages)

    def _thinking_mode_tool_rounds_need_nonstream(self) -> bool:
        """DeepSeek thinking + 工具：下一轮请求必须带回 assistant 的 ``reasoning_content``。

        官方文档要求带 tool_calls 时 ``reasoning_content`` 必须参与拼接；流式 delta
        在部分网关/SDK 组合下可能无法稳定汇总该字段，导致 400：
        ``The reasoning_content in the thinking mode must be passed back to the API``。
        非流式 ``choice.message`` 与官方示例一致，可一次取全 ``content`` / ``reasoning_content`` / ``tool_calls``。
        """
        if not self._capabilities.reasoning_content:
            return False
        vp = self._vendor_params or {}
        th = vp.get("thinking")
        if not isinstance(th, dict):
            return False
        return str(th.get("type", "")).strip().lower() == "enabled"

    @staticmethod
    def _reasoning_fragment_from_delta(delta: Any) -> Optional[str]:
        """从流式 delta 取一段 ``reasoning_content``（兼容 extra 字段与 model_dump）。"""
        if delta is None:
            return None
        rc = getattr(delta, "reasoning_content", None)
        if isinstance(rc, str) and rc:
            return rc
        extra = getattr(delta, "__pydantic_extra__", None)
        if isinstance(extra, dict):
            er = extra.get("reasoning_content")
            if isinstance(er, str) and er:
                return er
        try:
            dumped = delta.model_dump()
            if isinstance(dumped, dict):
                dr = dumped.get("reasoning_content")
                if isinstance(dr, str) and dr:
                    return dr
        except Exception:
            pass
        return None

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        system_message: Optional[str] = None,
    ) -> LLMResponse:
        full_messages: List[Dict[str, Any]] = []
        if system_message:
            full_messages.append({"role": "system", "content": system_message})
        full_messages.extend(self._prepare_openai_messages(messages))

        request_params: Dict[str, Any] = {
            "model": self._model,
            "messages": full_messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        self._apply_vendor_extra_body(request_params)

        if self._stream:
            return await self._chat_with_tools_stream(request_params)

        response = await self._client.chat.completions.create(**request_params)
        choice = response.choices[0]
        usage = TokenUsage.from_response(response)
        content = _strip_thinking_content(
            _normalize_text_content(choice.message.content)
        )
        rc = getattr(choice.message, "reasoning_content", None)
        reasoning_content = rc.strip() if isinstance(rc, str) and rc.strip() else None

        return LLMResponse(
            content=content,
            tool_calls=[],
            finish_reason=choice.finish_reason,
            raw_response=response,
            usage=usage,
            reasoning_content=reasoning_content,
        )

    async def chat_with_image(
        self,
        prompt: str,
        image_url: str,
        system_message: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> LLMResponse:
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
            "model": model_override or self._model,
            "messages": full_messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        self._apply_vendor_extra_body(request_params)

        response = await self._client.chat.completions.create(**request_params)
        choice = response.choices[0]
        usage = TokenUsage.from_response(response)
        content = _strip_thinking_content(
            _normalize_text_content(choice.message.content)
        )
        rc = getattr(choice.message, "reasoning_content", None)
        reasoning_content = rc.strip() if isinstance(rc, str) and rc.strip() else None

        return LLMResponse(
            content=content,
            tool_calls=[],
            finish_reason=choice.finish_reason,
            raw_response=response,
            usage=usage,
            reasoning_content=reasoning_content,
        )

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
        full_messages: List[Dict[str, Any]] = []
        if system_message:
            full_messages.append({"role": "system", "content": system_message})
        full_messages.extend(self._prepare_openai_messages(messages))

        effective_max_tokens = (
            int(max_tokens_override)
            if max_tokens_override and max_tokens_override > 0
            else self._max_tokens
        )
        request_params: Dict[str, Any] = {
            "model": self._model,
            "messages": full_messages,
            "temperature": self._temperature,
            "max_tokens": effective_max_tokens,
        }

        if tools and self._capabilities.function_calling:
            request_params["tools"] = tools
            request_params["tool_choice"] = tool_choice
            if self._capabilities.parallel_tool_calls:
                # 部分兼容网关不支持此参数，按 capabilities 控制是否下发
                request_params["parallel_tool_calls"] = True

        self._apply_vendor_extra_body(request_params)

        use_stream = self._stream and not self._thinking_mode_tool_rounds_need_nonstream()
        if use_stream:
            return await self._chat_with_tools_stream(
                request_params,
                on_content_delta=on_content_delta,
                on_reasoning_delta=on_reasoning_delta,
            )

        logger.debug(
            "OpenAICompatProvider[%s] chat.completions (stream=False) model=%s",
            self._name,
            self._model,
        )
        response = await self._client.chat.completions.create(**request_params)
        choice = response.choices[0]

        tool_calls: List[ToolCall] = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                        extra_content=_tool_call_extra_content(tc),
                    )
                )

        usage = TokenUsage.from_response(response)
        content = _strip_thinking_content(
            _normalize_text_content(choice.message.content)
        )
        content, tool_calls = _extract_tool_code_calls(content, tool_calls)

        rc = getattr(choice.message, "reasoning_content", None)
        reasoning_content = rc.strip() if isinstance(rc, str) and rc.strip() else None

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            raw_response=response,
            usage=usage,
            reasoning_content=reasoning_content,
        )

    async def _chat_with_tools_stream(
        self,
        request_params: Dict[str, Any],
        on_content_delta: Optional[Callable[[str], Any]] = None,
        on_reasoning_delta: Optional[Callable[[str], Any]] = None,
    ) -> LLMResponse:
        """汇总流式响应为完整 LLMResponse。"""
        params = {
            **request_params,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        logger.debug(
            "OpenAICompatProvider[%s] chat.completions (stream=True) model=%s",
            self._name,
            self._model,
        )
        stream = await self._client.chat.completions.create(**params)

        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls_map: Dict[int, Dict[str, Any]] = {}
        finish_reason = "stop"
        last_usage: Any = None
        filter_state: Dict[str, str] = {"mode": "normal", "pending": ""}

        async for chunk in stream:
            if not chunk.choices:
                if hasattr(chunk, "usage") and chunk.usage:
                    last_usage = chunk.usage
                continue

            delta = chunk.choices[0].delta
            if (
                hasattr(chunk.choices[0], "finish_reason")
                and chunk.choices[0].finish_reason
            ):
                finish_reason = chunk.choices[0].finish_reason

            if delta.content:
                content_parts.append(delta.content)
                if on_content_delta:
                    filtered = self._filter_thinking_delta(delta.content, filter_state)
                    if filtered:
                        maybe_awaitable = on_content_delta(filtered)
                        if inspect.isawaitable(maybe_awaitable):
                            await maybe_awaitable
            rfrag = OpenAICompatProvider._reasoning_fragment_from_delta(delta)
            if rfrag:
                reasoning_parts.append(rfrag)
                if on_reasoning_delta:
                    maybe_awaitable = on_reasoning_delta(rfrag)
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
                            "extra_content": _tool_call_extra_content(tc),
                        }
                    else:
                        if getattr(tc, "id", None):
                            tool_calls_map[idx]["id"] = tc.id
                        if hasattr(tc, "function") and tc.function:
                            if getattr(tc.function, "name", None):
                                tool_calls_map[idx]["name"] = tc.function.name
                            if getattr(tc.function, "arguments", None):
                                tool_calls_map[idx]["arguments"] += (
                                    tc.function.arguments or ""
                                )
                        ex = _tool_call_extra_content(tc)
                        if ex is not None:
                            tool_calls_map[idx]["extra_content"] = ex

            if hasattr(chunk, "usage") and chunk.usage:
                last_usage = chunk.usage

        raw_content = "".join(content_parts) if content_parts else None
        content = _strip_thinking_content(raw_content)

        tool_calls_list: List[ToolCall] = []
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            if tc["id"] and tc["name"]:
                raw = tc["arguments"] or ""
                ex = tc.get("extra_content")
                if not raw:
                    logger.warning(
                        "流式 tool_call 的 arguments 为空 name=%s id=%s",
                        tc["name"],
                        tc["id"],
                    )
                    tool_calls_list.append(
                        ToolCall(
                            id=tc["id"],
                            name=tc["name"],
                            arguments={},
                            extra_content=ex,
                        )
                    )
                    continue
                try:
                    args = json.loads(raw)
                except json.JSONDecodeError as e:
                    logger.warning(
                        "流式 tool_call arguments JSON 解析失败 name=%s id=%s len=%s err=%s preview=%s",
                        tc["name"],
                        tc["id"],
                        len(raw),
                        e,
                        raw[:300] if len(raw) > 300 else raw,
                    )
                    tool_calls_list.append(
                        ToolCall(
                            id=tc["id"],
                            name=tc["name"],
                            arguments=raw,
                            extra_content=ex,
                        )
                    )
                else:
                    tool_calls_list.append(
                        ToolCall(
                            id=tc["id"],
                            name=tc["name"],
                            arguments=args,
                            extra_content=ex,
                        )
                    )

        usage = TokenUsage.from_usage(last_usage) if last_usage else None

        content, tool_calls_list = _extract_tool_code_calls(content, tool_calls_list)

        reasoning_joined = "".join(reasoning_parts) if reasoning_parts else None
        reasoning_content = (
            reasoning_joined.strip()
            if reasoning_joined and reasoning_joined.strip()
            else None
        )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls_list,
            finish_reason=finish_reason,
            raw_response=None,
            usage=usage,
            reasoning_content=reasoning_content,
        )

    @staticmethod
    def _filter_thinking_delta(chunk: str, state: Dict[str, str]) -> str:
        """流式输出时剔除 <think>...</think> 段，仅返回可展示文本。"""
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

    async def close(self) -> None:
        await self._client.close()
