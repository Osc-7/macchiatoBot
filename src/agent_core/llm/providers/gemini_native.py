"""
Gemini Native GenerateContent API provider (v1beta).

对接 Google Gemini 原生 ``v1beta/models/{model}:generateContent`` 与
``:streamGenerateContent?alt=sse`` 端点（含 sub2api ``/antigravity`` 等兼容网关）。

核心设计：
- 原样保留模型返回的 ``parts``（含 ``thought: true`` 隐藏思考块与真实加密 ``thoughtSignature``）
  到 ``LLMResponse.gemini_message_content``，在后续多轮调用中无损放回 ``contents``。
- 当历史轮次来自其他协议/模型切换且缺少 ``gemini_message_content`` 时，从 ``extra_content``
  恢复签名或在首个 ``functionCall`` 上回退官方 ``skip_thought_signature_validator``。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import inspect
import json
import logging
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from agent_core.llm.capabilities import Capabilities
from agent_core.llm.providers.openai_compat import (
    _GEMINI_DUMMY_THOUGHT_SIGNATURE_SKIP,
    _merge_inline_thinking,
)
from agent_core.llm.response import LLMResponse, TokenUsage, ToolCall

from .base import BaseProvider

logger = logging.getLogger(__name__)
_SSE_DATA_RE = re.compile(r"^data:\s?(.*)")
_DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,(.+)$", re.DOTALL)

_UNSUPPORTED_SCHEMA_KEYS = {
    "additionalProperties",
    "$schema",
    "default",
    "title",
    "examples",
}

_RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504, 520, 522, 524}
_RETRYABLE_STREAM_ERROR_MARKERS = (
    "stream_timeout",
    "timeout",
    "deadline_exceeded",
    "unavailable",
    "overloaded",
    "internal",
    "bad gateway",
    "gateway timeout",
    "resource_exhausted",
    "rate limit",
    "rate_limit",
)


class _GeminiStreamError(RuntimeError):
    """SSE 流内返回的 error 事件。"""

    def __init__(self, message: str, *, code: Optional[int] = None, status: str = "") -> None:
        super().__init__(f"Gemini stream error: {message}")
        self.code = code
        self.status = status
        self.raw_message = message


def _is_retryable_gemini_exception(exc: BaseException) -> bool:
    """判断是否为可自动重试的网关/网络/流式超时瞬态错误。"""
    if isinstance(
        exc,
        (
            httpx.RemoteProtocolError,
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.WriteError,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_HTTP_STATUS
    if isinstance(exc, _GeminiStreamError):
        if exc.code in _RETRYABLE_HTTP_STATUS:
            return True
        combined = f"{exc.status} {exc.raw_message}".lower()
        return any(m in combined for m in _RETRYABLE_STREAM_ERROR_MARKERS)
    return False


def _content_delta_has_visible_text(existing_parts: List[str], delta: str) -> bool:
    if not isinstance(delta, str) or not delta:
        return False
    return bool(("".join(existing_parts) + delta).strip())


def _clean_json_schema_for_gemini(schema: Any) -> Any:
    """清理 OpenAI / MCP JSON Schema 中 Gemini FunctionDeclaration 不接受的扩展键。"""
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for key, val in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key == "type" and isinstance(val, list):
            non_null = [str(x) for x in val if x != "null"]
            out["type"] = non_null[0] if non_null else "string"
            if "null" in val:
                out["nullable"] = True
            continue
        if key == "properties" and isinstance(val, dict):
            out[key] = {
                str(pk): _clean_json_schema_for_gemini(pv)
                for pk, pv in val.items()
                if isinstance(pv, dict)
            }
        elif key == "items":
            if isinstance(val, dict):
                out[key] = _clean_json_schema_for_gemini(val)
            elif isinstance(val, list) and val:
                out[key] = _clean_json_schema_for_gemini(val[0])
        elif key in ("anyOf", "oneOf", "allOf") and isinstance(val, list):
            out[key] = [_clean_json_schema_for_gemini(item) for item in val]
        else:
            out[key] = val

    if out.get("type") == "object":
        props = out.get("properties")
        if not isinstance(props, dict) or not props:
            out["properties"] = {}
            out.pop("required", None)
        elif isinstance(out.get("required"), list):
            out["required"] = [r for r in out["required"] if r in props]
            if not out["required"]:
                out.pop("required", None)
    return out


def _parse_data_url(url: str) -> Optional[Tuple[str, str]]:
    m = _DATA_URL_RE.match((url or "").strip())
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def _parse_tool_args_from_openai_tc(tc: Dict[str, Any]) -> Dict[str, Any]:
    fn = tc.get("function") or {}
    raw = fn.get("arguments", {})
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else {"_value": parsed}
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {}


def _extract_signature_from_openai_tc(tc: Dict[str, Any]) -> Optional[str]:
    ex = tc.get("extra_content")
    if not isinstance(ex, dict):
        return None
    g = ex.get("google")
    if not isinstance(g, dict):
        return None
    sig = g.get("thought_signature") or g.get("thoughtSignature")
    if isinstance(sig, str) and sig.strip():
        return sig.strip()
    return None


def _build_function_response_part(
    *,
    tool_call_id: str,
    tool_name: str,
    raw_content: Any,
) -> Dict[str, Any]:
    if isinstance(raw_content, dict):
        resp_obj: Dict[str, Any] = raw_content
    elif isinstance(raw_content, str):
        text = raw_content.strip()
        resp_obj = {"output": raw_content}
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    resp_obj = parsed
            except json.JSONDecodeError:
                pass
    else:
        resp_obj = {"output": str(raw_content)}

    fr: Dict[str, Any] = {
        "name": tool_name or "tool",
        "response": resp_obj,
    }
    if tool_call_id:
        fr["id"] = tool_call_id
    return {"functionResponse": fr}


def _merge_adjacent_same_role_contents(
    contents: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for item in contents:
        role = item.get("role")
        parts = item.get("parts") or []
        if not parts:
            continue
        if merged and merged[-1].get("role") == role:
            merged[-1]["parts"].extend(parts)
        else:
            merged.append({"role": role, "parts": list(parts)})
    return merged


def _coalesce_streamed_gemini_parts(raw_parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并流式 SSE 下发的相邻同类型纯文本/思考碎片，保持签名与工具调用块边界不变。"""
    out: List[Dict[str, Any]] = []
    for part in raw_parts:
        if not isinstance(part, dict):
            continue
        p = copy.deepcopy(part)
        # 流结束尾包常见空 {"text": ""}（不带 thought/signature/functionCall），跳过以免污染
        if (
            set(p.keys()) == {"text"}
            and isinstance(p.get("text"), str)
            and p["text"] == ""
            and out
        ):
            continue

        is_pure_thought = (
            bool(p.get("thought"))
            and "text" in p
            and "functionCall" not in p
            and "thoughtSignature" not in p
        )
        is_pure_text = (
            not p.get("thought")
            and "text" in p
            and "functionCall" not in p
            and "thoughtSignature" not in p
            and "inlineData" not in p
            and "fileData" not in p
        )
        if out:
            prev = out[-1]
            prev_pure_thought = (
                bool(prev.get("thought"))
                and "text" in prev
                and "functionCall" not in prev
                and "thoughtSignature" not in prev
            )
            prev_pure_text = (
                not prev.get("thought")
                and "text" in prev
                and "functionCall" not in prev
                and "thoughtSignature" not in prev
                and "inlineData" not in prev
                and "fileData" not in prev
            )
            if is_pure_thought and prev_pure_thought:
                prev["text"] = str(prev.get("text") or "") + str(p.get("text") or "")
                continue
            if is_pure_text and prev_pure_text:
                prev["text"] = str(prev.get("text") or "") + str(p.get("text") or "")
                continue
        out.append(p)
    return out


class GeminiNativeProvider(BaseProvider):
    """对接 Gemini v1beta 原生 GenerateContent / streamGenerateContent 的 provider。"""

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

        raw_base = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
        if raw_base.endswith("/v1beta/openai"):
            raw_base = raw_base[: -len("/openai")]
        elif not raw_base.endswith("/v1beta"):
            raw_base = f"{raw_base}/v1beta"
        self._api_base = raw_base
        self._api_key = api_key

        http_headers = dict(headers or {})
        trust_env = bool(self._vendor_params.pop("trust_env", True))
        self._http_client = httpx.AsyncClient(
            headers=http_headers,
            timeout=request_timeout_seconds,
            trust_env=trust_env,
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
        if self._capabilities.context_window is not None:
            return self._capabilities.context_window
        return 1_000_000

    @property
    def temperature(self) -> float:
        return self._temperature

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def _build_auth_headers(self) -> Dict[str, str]:
        headers = {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        headers.update(self._http_client.headers)
        return headers

    def _model_endpoint(self, action: str, *, model_override: Optional[str] = None) -> str:
        m = (model_override or self._model).strip()
        if m.startswith("models/"):
            m = m[len("models/") :]
        return f"{self._api_base}/models/{m}:{action}"

    async def _fetch_image_inline_data(self, url: str) -> Optional[Dict[str, Any]]:
        parsed = _parse_data_url(url)
        if parsed is not None:
            mime, b64 = parsed
            return {"inlineData": {"mimeType": mime, "data": b64}}
        if url.startswith(("http://", "https://")):
            try:
                resp = await self._http_client.get(url, timeout=30.0)
                resp.raise_for_status()
                mime = (
                    resp.headers.get("content-type", "image/jpeg")
                    .split(";")[0]
                    .strip()
                    or "image/jpeg"
                )
                b64 = base64.b64encode(resp.content).decode("ascii")
                return {"inlineData": {"mimeType": mime, "data": b64}}
            except Exception as exc:
                logger.warning("GeminiNativeProvider[%s] 拉取图片 URL 失败: %s", self._name, exc)
                return {
                    "text": "[系统提示：此处图片拉取失败，未能送达模型，请基于其余内容作答。]"
                }
        return None

    async def _convert_user_content_to_parts(self, content: Any) -> List[Dict[str, Any]]:
        if isinstance(content, str):
            return [{"text": content}] if content else []
        if not isinstance(content, list):
            s = str(content or "")
            return [{"text": s}] if s else []

        parts: List[Dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            ptype = item.get("type")
            if ptype == "text":
                txt = str(item.get("text") or "")
                if txt:
                    parts.append({"text": txt})
            elif ptype == "image_url":
                img_url = str((item.get("image_url") or {}).get("url") or "").strip()
                if img_url:
                    img_part = await self._fetch_image_inline_data(img_url)
                    if img_part is not None:
                        parts.append(img_part)
            elif ptype == "file":
                fobj = item.get("file") or {}
                fdata = str(fobj.get("file_data") or "").strip()
                mime = str(item.get("mime_type") or "application/pdf").strip()
                if fdata:
                    parts.append({"inlineData": {"mimeType": mime, "data": fdata}})
        return parts

    async def _convert_messages(
        self,
        messages: List[Dict[str, Any]],
        system_message: Optional[str] = None,
    ) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        contents: List[Dict[str, Any]] = []
        n = len(messages)
        consumed = [False] * n
        call_id_to_name: Dict[str, str] = {}

        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        tid = str(tc.get("id") or "").strip()
                        fn_name = str((tc.get("function") or {}).get("name") or "").strip()
                        if tid and fn_name:
                            call_id_to_name[tid] = fn_name

        i = 0
        while i < n:
            if consumed[i]:
                i += 1
                continue

            msg = messages[i]
            role = msg.get("role")
            content = msg.get("content")

            if role == "system":
                if isinstance(content, str) and content.strip():
                    system_message = (
                        f"{system_message}\n{content}" if system_message else content
                    )
                i += 1
                continue

            if role == "assistant":
                raw_gmc = msg.get("gemini_message_content")
                tool_calls = [tc for tc in (msg.get("tool_calls") or []) if isinstance(tc, dict)]
                ids_order = [
                    str(tc.get("id") or "").strip()
                    for tc in tool_calls
                    if str(tc.get("id") or "").strip()
                ]

                model_parts: List[Dict[str, Any]] = []
                if isinstance(raw_gmc, list) and raw_gmc:
                    model_parts = copy.deepcopy(raw_gmc)
                else:
                    rc = str(msg.get("reasoning_content") or "").strip()
                    if rc:
                        model_parts.append({"thought": True, "text": rc})
                    if isinstance(content, str) and content:
                        model_parts.append({"text": content})
                    first_fc = True
                    for tc in tool_calls:
                        tid = str(tc.get("id") or "").strip()
                        fn_name = str((tc.get("function") or {}).get("name") or "").strip()
                        if not fn_name:
                            continue
                        fc_obj: Dict[str, Any] = {
                            "name": fn_name,
                            "args": _parse_tool_args_from_openai_tc(tc),
                        }
                        if tid:
                            fc_obj["id"] = tid
                        part_obj: Dict[str, Any] = {"functionCall": fc_obj}
                        sig = _extract_signature_from_openai_tc(tc)
                        if sig:
                            part_obj["thoughtSignature"] = sig
                        elif first_fc:
                            # 仅在跨协议切换/历史无真签名时用官方占位符兜底首个并行调用
                            part_obj["thoughtSignature"] = _GEMINI_DUMMY_THOUGHT_SIGNATURE_SKIP
                        first_fc = False
                        model_parts.append(part_obj)

                if model_parts:
                    contents.append({"role": "model", "parts": model_parts})

                if ids_order:
                    expected = set(ids_order)
                    tool_body: Dict[str, Any] = {}
                    j = i + 1
                    while j < n and messages[j].get("role") != "assistant":
                        if messages[j].get("role") == "tool":
                            tid = str(messages[j].get("tool_call_id") or "").strip()
                            if tid in expected:
                                tool_body[tid] = messages[j].get("content", "")
                                consumed[j] = True
                        j += 1
                    resp_parts: List[Dict[str, Any]] = []
                    for tid in ids_order:
                        if tid in tool_body:
                            resp_parts.append(
                                _build_function_response_part(
                                    tool_call_id=tid,
                                    tool_name=call_id_to_name.get(tid, "tool"),
                                    raw_content=tool_body[tid],
                                )
                            )
                    if resp_parts:
                        contents.append({"role": "user", "parts": resp_parts})
                i += 1
                continue

            if role == "tool":
                tid = str(msg.get("tool_call_id") or "").strip()
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            _build_function_response_part(
                                tool_call_id=tid,
                                tool_name=call_id_to_name.get(tid, "tool"),
                                raw_content=msg.get("content", ""),
                            )
                        ],
                    }
                )
                i += 1
                continue

            if role == "user":
                uparts = await self._convert_user_content_to_parts(content)
                if uparts:
                    contents.append({"role": "user", "parts": uparts})
                i += 1
                continue

            i += 1

        merged = _merge_adjacent_same_role_contents(contents)
        return system_message, merged

    def _convert_tools(
        self,
        tools: Optional[List[Dict[str, Any]]],
    ) -> Optional[List[Dict[str, Any]]]:
        if not tools:
            return None
        decls: List[Dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                continue
            fn = tool.get("function") or {}
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            raw_params = fn.get("parameters") or {"type": "object", "properties": {}}
            cleaned_params = _clean_json_schema_for_gemini(raw_params)
            decls.append(
                {
                    "name": name,
                    "description": str(fn.get("description") or ""),
                    "parameters": cleaned_params,
                }
            )
        if not decls:
            return None
        return [{"functionDeclarations": decls}]

    def _build_payload(
        self,
        *,
        contents: List[Dict[str, Any]],
        system_instruction: Optional[str],
        gemini_tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        max_tokens_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        effective_max_tokens = (
            int(max_tokens_override)
            if max_tokens_override and max_tokens_override > 0
            else self._max_tokens
        )
        gen_cfg: Dict[str, Any] = {
            "maxOutputTokens": effective_max_tokens,
            "temperature": self._temperature,
        }
        if self._capabilities.reasoning_content:
            thinking_cfg: Dict[str, Any] = {"includeThoughts": True}
            m_low = self._model.lower()
            if m_low.endswith("-high"):
                thinking_cfg["thinkingLevel"] = "HIGH"
            elif m_low.endswith("-medium"):
                thinking_cfg["thinkingLevel"] = "MEDIUM"
            elif m_low.endswith("-low"):
                thinking_cfg["thinkingLevel"] = "LOW"
            gen_cfg["thinkingConfig"] = thinking_cfg

        # vendor_params 支持覆盖 generationConfig（如 thinkingConfig / temperature 等）或顶层参数
        vp = copy.deepcopy(self._vendor_params)
        if "temperature" in vp:
            tval = vp.pop("temperature")
            if tval is None:
                gen_cfg.pop("temperature", None)
            else:
                gen_cfg["temperature"] = tval
        if "thinkingConfig" in vp and isinstance(vp["thinkingConfig"], dict):
            base_tc = dict(gen_cfg.get("thinkingConfig") or {})
            base_tc.update(vp.pop("thinkingConfig"))
            gen_cfg["thinkingConfig"] = base_tc
        if "generationConfig" in vp and isinstance(vp["generationConfig"], dict):
            extra_gc = vp.pop("generationConfig")
            if "thinkingConfig" in extra_gc and isinstance(extra_gc["thinkingConfig"], dict):
                base_tc = dict(gen_cfg.get("thinkingConfig") or {})
                base_tc.update(extra_gc.pop("thinkingConfig"))
                gen_cfg["thinkingConfig"] = base_tc
            gen_cfg.update(extra_gc)

        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": gen_cfg,
        }
        if system_instruction:
            payload["systemInstruction"] = {
                "parts": [{"text": system_instruction}],
            }
        if gemini_tools and self._capabilities.function_calling:
            payload["tools"] = gemini_tools
            tc_norm = (tool_choice or "auto").strip().lower()
            if tc_norm == "auto":
                payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
            elif tc_norm == "required":
                payload["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
            elif tc_norm == "none":
                payload["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
            else:
                payload["toolConfig"] = {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [tool_choice],
                    }
                }
        if vp:
            payload.update(vp)
        return payload

    def _finalize_from_parts(
        self,
        raw_parts: List[Dict[str, Any]],
        *,
        finish_reason: str,
        usage_meta: Dict[str, Any],
        raw_response: Any = None,
    ) -> LLMResponse:
        coalesced = _coalesce_streamed_gemini_parts(raw_parts)
        text_chunks: List[str] = []
        thought_chunks: List[str] = []
        tool_calls: List[ToolCall] = []

        for part in coalesced:
            if not isinstance(part, dict):
                continue
            if part.get("thought") is True:
                t = part.get("text")
                if isinstance(t, str) and t:
                    thought_chunks.append(t)
                continue
            fc = part.get("functionCall")
            if isinstance(fc, dict):
                fname = str(fc.get("name") or "").strip()
                if fname:
                    fid = str(fc.get("id") or "").strip() or f"call_{uuid.uuid4().hex[:16]}"
                    # 确保写回 coalesced 的 functionCall 也有一致 id，供下一轮 functionResponse 对齐
                    fc["id"] = fid
                    fargs = fc.get("args")
                    if not isinstance(fargs, dict):
                        fargs = {}
                    sig = part.get("thoughtSignature")
                    extra: Optional[Dict[str, Any]] = None
                    if isinstance(sig, str) and sig.strip():
                        extra = {"google": {"thought_signature": sig.strip()}}
                    tool_calls.append(
                        ToolCall(
                            id=fid,
                            name=fname,
                            arguments=fargs,
                            extra_content=extra,
                        )
                    )
            if "text" in part and not part.get("thought"):
                t = part.get("text")
                if isinstance(t, str) and t:
                    text_chunks.append(t)

        content: Optional[str] = "".join(text_chunks).strip() or None
        reasoning: Optional[str] = "".join(thought_chunks).strip() or None
        if content and self._capabilities.thinking_tag_inline:
            content, reasoning = _merge_inline_thinking(content, reasoning)

        prompt_tok = int(usage_meta.get("promptTokenCount") or 0)
        cand_tok = int(usage_meta.get("candidatesTokenCount") or 0)
        thoughts_tok = int(usage_meta.get("thoughtsTokenCount") or 0)
        total_tok = int(
            usage_meta.get("totalTokenCount") or (prompt_tok + cand_tok + thoughts_tok)
        )
        cached_tok = int(usage_meta.get("cachedContentTokenCount") or 0)
        usage = TokenUsage(
            prompt_tokens=prompt_tok,
            completion_tokens=cand_tok + thoughts_tok,
            total_tokens=total_tok,
            prompt_cache_hit_tokens=cached_tok,
            prompt_cache_miss_tokens=max(0, prompt_tok - cached_tok),
        )

        fr_norm = (finish_reason or "STOP").strip().lower()
        if fr_norm == "max_tokens":
            fr_norm = "length"
        elif fr_norm == "stop":
            fr_norm = "stop"

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=fr_norm,
            raw_response=raw_response,
            usage=usage,
            reasoning_content=reasoning,
            gemini_message_content=coalesced if coalesced else None,
        )

    async def _post_json(
        self,
        url: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        headers = self._build_auth_headers()
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                resp = await self._http_client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                if not _is_retryable_gemini_exception(exc) or attempt >= 2:
                    raise
                last_exc = exc
                delay = float(2**attempt)
                logger.warning(
                    "GeminiNativeProvider[%s] 请求瞬时错误 (%r)，%.0fs 后重试 (%d/2)",
                    self._name,
                    exc,
                    delay,
                    attempt + 1,
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    async def _stream_request(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        on_content_delta: Optional[Callable[[str], Any]] = None,
        on_reasoning_delta: Optional[Callable[[str], Any]] = None,
    ) -> LLMResponse:
        headers = self._build_auth_headers()
        raw_parts: List[Dict[str, Any]] = []
        visible_text_parts: List[str] = []
        finish_reason = "STOP"
        usage_meta: Dict[str, Any] = {}
        emitted_visible_content = False

        async def _emit(cb: Optional[Callable[[str], Any]], delta: str) -> None:
            if not cb or not delta:
                return
            maybe = cb(delta)
            if inspect.isawaitable(maybe):
                await maybe

        async def _handle_sse_block(block: str) -> None:
            nonlocal finish_reason, usage_meta, emitted_visible_content
            if not block.strip():
                return
            data_lines: List[str] = []
            for line in block.splitlines():
                line = line.rstrip("\r")
                m = _SSE_DATA_RE.match(line)
                if m:
                    data_lines.append(m.group(1))
            if not data_lines:
                return
            data_str = "\n".join(data_lines).strip()
            if not data_str or data_str == "[DONE]":
                return
            try:
                event_obj = json.loads(data_str)
            except json.JSONDecodeError:
                logger.debug("忽略无法解析的 Gemini SSE data: %r", data_str[:300])
                return

            if isinstance(event_obj, dict) and "error" in event_obj:
                err = event_obj["error"]
                if isinstance(err, dict):
                    msg = str(err.get("message") or err)
                    raw_code = err.get("code")
                    code = int(raw_code) if isinstance(raw_code, int) else None
                    status = str(err.get("status") or "")
                else:
                    msg = str(err)
                    code = None
                    status = ""
                raise _GeminiStreamError(msg, code=code, status=status)

            um = event_obj.get("usageMetadata") if isinstance(event_obj, dict) else None
            if isinstance(um, dict):
                usage_meta.update(um)

            cands = event_obj.get("candidates") if isinstance(event_obj, dict) else None
            if not isinstance(cands, list) or not cands:
                return
            cand0 = cands[0]
            if not isinstance(cand0, dict):
                return
            fr = cand0.get("finishReason")
            if isinstance(fr, str) and fr.strip():
                finish_reason = fr.strip()
            content_obj = cand0.get("content")
            if not isinstance(content_obj, dict):
                return
            parts = content_obj.get("parts")
            if not isinstance(parts, list):
                return
            for part in parts:
                if not isinstance(part, dict):
                    continue
                raw_parts.append(copy.deepcopy(part))
                if part.get("thought") is True:
                    t = part.get("text")
                    if isinstance(t, str) and t:
                        await _emit(on_reasoning_delta, t)
                elif "text" in part and not part.get("thought"):
                    t = part.get("text")
                    if isinstance(t, str) and t:
                        emit = _content_delta_has_visible_text(visible_text_parts, t)
                        visible_text_parts.append(t)
                        if emit:
                            if on_content_delta is not None:
                                emitted_visible_content = True
                            await _emit(on_content_delta, t)

        async def _stream_once() -> None:
            nonlocal finish_reason
            raw_parts.clear()
            visible_text_parts.clear()
            finish_reason = "STOP"
            usage_meta.clear()
            async with self._http_client.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    response.raise_for_status()
                buffer = ""
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    buffer += chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
                    while "\n\n" in buffer:
                        block, buffer = buffer.split("\n\n", 1)
                        await _handle_sse_block(block)
                if buffer.strip():
                    await _handle_sse_block(buffer)

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                await _stream_once()
                break
            except Exception as exc:
                # 只要尚未向前端推送任何可见正文，502/504 或 SSE stream_timeout 均可无损清空重连
                if (
                    emitted_visible_content
                    or attempt >= 2
                    or not _is_retryable_gemini_exception(exc)
                ):
                    raise
                last_exc = exc
                delay = float(2**attempt)
                logger.warning(
                    "GeminiNativeProvider[%s] 流式瞬时错误 (%r)，%.0fs 后重试 (%d/2)",
                    self._name,
                    exc,
                    delay,
                    attempt + 1,
                )
                await asyncio.sleep(delay)
        else:
            if last_exc is not None:
                raise last_exc

        return self._finalize_from_parts(
            raw_parts,
            finish_reason=finish_reason,
            usage_meta=usage_meta,
        )

    def _parse_nonstream_response(self, data: Dict[str, Any]) -> LLMResponse:
        usage_meta = data.get("usageMetadata") if isinstance(data, dict) else None
        cands = data.get("candidates") if isinstance(data, dict) else None
        raw_parts: List[Dict[str, Any]] = []
        finish_reason = "STOP"
        if isinstance(cands, list) and cands and isinstance(cands[0], dict):
            cand0 = cands[0]
            fr = cand0.get("finishReason")
            if isinstance(fr, str) and fr.strip():
                finish_reason = fr.strip()
            cobj = cand0.get("content")
            if isinstance(cobj, dict) and isinstance(cobj.get("parts"), list):
                raw_parts = copy.deepcopy(cobj["parts"])
        return self._finalize_from_parts(
            raw_parts,
            finish_reason=finish_reason,
            usage_meta=usage_meta if isinstance(usage_meta, dict) else {},
            raw_response=data,
        )

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        system_message: Optional[str] = None,
    ) -> LLMResponse:
        sys_inst, contents = await self._convert_messages(messages, system_message)
        payload = self._build_payload(
            contents=contents,
            system_instruction=sys_inst,
        )
        if self._stream:
            url = f"{self._model_endpoint('streamGenerateContent')}?alt=sse"
            return await self._stream_request(url, payload)
        url = self._model_endpoint("generateContent")
        data = await self._post_json(url, payload)
        return self._parse_nonstream_response(data)

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
        sys_inst, contents = await self._convert_messages(messages, system_message)
        gemini_tools = self._convert_tools(tools)
        payload = self._build_payload(
            contents=contents,
            system_instruction=sys_inst,
            gemini_tools=gemini_tools,
            tool_choice=tool_choice,
            max_tokens_override=max_tokens_override,
        )
        if self._stream:
            url = f"{self._model_endpoint('streamGenerateContent')}?alt=sse"
            return await self._stream_request(
                url,
                payload,
                on_content_delta=on_content_delta,
                on_reasoning_delta=on_reasoning_delta,
            )
        url = self._model_endpoint("generateContent")
        data = await self._post_json(url, payload)
        return self._parse_nonstream_response(data)

    async def chat_with_image(
        self,
        prompt: str,
        image_url: str,
        system_message: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> LLMResponse:
        parts: List[Dict[str, Any]] = [{"text": prompt}]
        img_part = await self._fetch_image_inline_data(image_url)
        if img_part is not None:
            parts.append(img_part)
        payload = self._build_payload(
            contents=[{"role": "user", "parts": parts}],
            system_instruction=system_message,
        )
        url = self._model_endpoint("generateContent", model_override=model_override)
        data = await self._post_json(url, payload)
        return self._parse_nonstream_response(data)

    async def close(self) -> None:
        await self._http_client.aclose()
