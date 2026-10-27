"""把 LLM 调用异常归一成结构化错误信息。

目的有三个：
1. 前端能拿到「这是什么错、要不要提示用户重试、给 owner 报什么」；
2. 公共渠道（水源）只取 :meth:`LLMErrorInfo.public_hint`，绝不把 provider
   名字、HTTP 状态码、上游响应体泄露到论坛；
3. owner 通知卡片取 :meth:`LLMErrorInfo.to_dict`，包含完整诊断字段。

分类尽量不依赖 openai 包的具体异常类型（只做 duck typing），因为
``anthropic_compat`` / ``codex_oauth_provider`` 抛的是各自的异常。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

# ── 语义分类常量 ──────────────────────────────────────────────────────────
KIND_RATE_LIMIT = "rate_limit"            # 429 / 短时并发限制
KIND_QUOTA = "quota_exhausted"            # 429 且上游明确说额度/容量耗尽
KIND_AUTH = "auth"                        # 401 / key 无效
KIND_FORBIDDEN = "forbidden"              # 403 / 地区封锁、WAF、CloudFront
KIND_BAD_REQUEST = "bad_request"          # 400 / 请求体非法
KIND_CONTEXT_OVERFLOW = "context_overflow"  # 400 且提示超出上下文
KIND_SERVER = "server"                    # 5xx
KIND_TIMEOUT = "timeout"                  # 读超时
KIND_CONNECTION = "connection"            # 连接失败 / DNS / TLS
KIND_STREAM_ERROR = "stream_error"        # HTTP 2xx 后流内推 error 事件
KIND_REFUSAL = "refusal"                  # 模型拒答文本被包装成 error
KIND_CANCELLED = "cancelled"
KIND_UNKNOWN = "unknown"

_RETRYABLE = {
    KIND_RATE_LIMIT,
    KIND_QUOTA,
    KIND_SERVER,
    KIND_TIMEOUT,
    KIND_CONNECTION,
    KIND_STREAM_ERROR,
}

_KIND_LABELS = {
    KIND_RATE_LIMIT: "上游限流",
    KIND_QUOTA: "上游额度耗尽",
    KIND_AUTH: "鉴权失败",
    KIND_FORBIDDEN: "上游拒绝访问（地区/网络封锁）",
    KIND_BAD_REQUEST: "请求被上游拒绝",
    KIND_CONTEXT_OVERFLOW: "上下文超出模型窗口",
    KIND_SERVER: "上游服务错误",
    KIND_TIMEOUT: "请求超时",
    KIND_CONNECTION: "网络连接失败",
    KIND_STREAM_ERROR: "流式响应中途断开",
    KIND_REFUSAL: "模型拒答（被包装为错误）",
    KIND_CANCELLED: "请求被取消",
    KIND_UNKNOWN: "未知错误",
}

_OPERATOR_HINTS = {
    KIND_RATE_LIMIT: "短时并发/限流，通常几十秒到几分钟恢复；连续出现可考虑降级到其它 provider。",
    KIND_QUOTA: "免费额度或套餐容量已用尽，需要等额度恢复、换套餐或换 provider。",
    KIND_AUTH: "API key 无效/过期/未配置，需要更新 providers.d 里的 key 或环境变量。",
    KIND_FORBIDDEN: "上游按出口 IP 或地区做了拒绝（常见于 CloudFront geo-block、WAF）。需要换代理节点或换 provider。",
    KIND_BAD_REQUEST: "请求体被上游判为非法，通常是参数/模型名/vendor_params 不兼容。",
    KIND_CONTEXT_OVERFLOW: "prompt + 工具结果超出窗口，需要压缩历史或调小工具输出。",
    KIND_SERVER: "上游 5xx，通常是对方故障；可重试，持续出现应换 provider。",
    KIND_TIMEOUT: "请求超时，检查网络与代理，或调大 llm.request_timeout_seconds。",
    KIND_CONNECTION: "连不上上游，检查网络、代理、DNS。",
    KIND_STREAM_ERROR: "HTTP 200 已建流后上游在流中推了 error 事件（SDK 不会自动重试这一层）。",
    KIND_REFUSAL: "模型按内容策略拒答，拒答文本被塞进 error 字段。换问法或换 provider。",
    KIND_CANCELLED: "请求被取消。",
    KIND_UNKNOWN: "未识别的异常，需要看堆栈。",
}

# 公共渠道（水源）可见的兜底文案：不包含任何 provider / 状态码 / 内部术语。
_PUBLIC_HINTS = {
    KIND_RATE_LIMIT: "我这会儿有点卡，脑子转不过来，稍后再来。",
    KIND_QUOTA: "我这会儿有点卡，脑子转不过来，稍后再来。",
    KIND_SERVER: "我这会儿有点卡，脑子转不过来，稍后再来。",
    KIND_TIMEOUT: "刚才没接上话，稍等一下再来。",
    KIND_CONNECTION: "刚才没接上话，稍等一下再来。",
    KIND_STREAM_ERROR: "刚才话没说完就断了，我再看一眼。",
    KIND_REFUSAL: "这句我暂时答不了，换个问法也许行。",
    KIND_CONTEXT_OVERFLOW: "这条太长了，我抓不住重点，拆小一点再问我一次？",
    KIND_AUTH: "我这边临时出了点状况，已经通知主人了。",
    KIND_FORBIDDEN: "我这边临时出了点状况，已经通知主人了。",
    KIND_BAD_REQUEST: "我这边临时出了点状况，已经通知主人了。",
    KIND_CANCELLED: "刚才那句先欠着。",
    KIND_UNKNOWN: "我这边临时出了点状况，已经通知主人了。",
}

_MAX_MESSAGE_CHARS = 600


@dataclass
class LLMErrorInfo:
    """一次失败的完整描述（可直接序列化进 session metadata）。"""

    kind: str = KIND_UNKNOWN
    label: str = ""
    message: str = ""
    http_status: Optional[int] = None
    error_code: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    exception_type: str = ""
    retryable: bool = False
    occurred_at: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    hint: str = ""
    request_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.label:
            self.label = _KIND_LABELS.get(self.kind, self.kind)
        if not self.hint:
            self.hint = _OPERATOR_HINTS.get(self.kind, "")

    @property
    def public_hint(self) -> str:
        """公共渠道兜底文案：不含任何内部细节。"""
        return _PUBLIC_HINTS.get(self.kind, _PUBLIC_HINTS[KIND_UNKNOWN])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def operator_line(self) -> str:
        """单行诊断摘要，供日志/短消息使用。"""
        bits = [self.label]
        if self.http_status:
            bits.append(f"HTTP {self.http_status}")
        if self.error_code:
            bits.append(str(self.error_code))
        if self.provider:
            bits.append(f"provider={self.provider}")
        return " / ".join(bits)


def _text_of(exc: BaseException) -> str:
    text = (str(exc) or "").strip()
    if not text:
        args = getattr(exc, "args", ()) or ()
        bits = [str(a) for a in args if str(a).strip()]
        text = f"{type(exc).__name__}: {', '.join(bits)}" if bits else type(exc).__name__
    return text


def _body_dict(exc: BaseException) -> Dict[str, Any]:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return err
        return body
    return {}


def _looks_like_html(text: str) -> bool:
    low = text.lower()
    return "<html" in low or "<!doctype" in low or "cloudfront" in low


_OVERFLOW_MARKERS = (
    "context length",
    "maximum context",
    "too many tokens",
    "context window",
)


def _is_sdk_api_error(exc: BaseException, type_name: str, message: str) -> bool:
    """无 HTTP 状态时，只有 LLM SDK 层异常才可能是「流内 error 事件」。

    openai 的异常都带 ``request``；anthropic/codex 的异常名带 API/HTTP 字样。
    普通业务异常（ValueError/RuntimeError 等）不应被误判成流式中断。
    """
    if getattr(exc, "request", None) is not None:
        return True
    if type_name in (
        "APIError",
        "APIStatusError",
        "APIConnectionError",
        "APITimeoutError",
        "StreamAPIError",
        "APIResponseValidationError",
    ):
        return True
    low = message.strip().lower()
    return low.startswith("error code:") or low.startswith("error code ")


def _looks_like_refusal(message: str) -> bool:
    """流内 error 事件里塞的是模型自然语言（而非 JSON 错误结构）。"""
    text = message.strip()
    if not text:
        return False
    if text.startswith("{") or text.startswith("Error code:"):
        return False
    if _looks_like_html(text):
        return False
    # 有句子的形态：长度够、含空格或标点
    if len(text) < 24:
        return False
    return (" " in text) or ("。" in text) or ("," in text)


def classify_llm_error(
    exc: BaseException,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    attempts: int = 1,
) -> LLMErrorInfo:
    """把异常归一成 :class:`LLMErrorInfo`。"""
    type_name = type(exc).__name__
    message = _text_of(exc)[:_MAX_MESSAGE_CHARS]
    body = _body_dict(exc)
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None

    code = None
    for key in ("code", "type"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            code = val.strip()
            break

    req = getattr(exc, "request", None)
    req_id = None
    if req is not None:
        headers = getattr(req, "headers", None) or {}
        try:
            req_id = headers.get("x-request-id") or headers.get("request-id")
        except Exception:  # noqa: BLE001
            req_id = None

    kind = KIND_UNKNOWN
    low = message.lower()

    if type_name in ("CancelledError",) or isinstance(exc, BaseException) and type_name == "CancelledError":
        kind = KIND_CANCELLED
    elif status == 429:
        quota_markers = ("quota", "capacity", "free tier", "exhausted", "insufficient")
        kind = KIND_QUOTA if any(m in low for m in quota_markers) else KIND_RATE_LIMIT
    elif status == 401:
        kind = KIND_AUTH
    elif status == 403:
        kind = KIND_FORBIDDEN
        if _looks_like_html(message):
            message = "上游返回 HTML 拦截页（疑似 CloudFront/geo-block）: " + message[:200]
    elif status == 404:
        kind = KIND_BAD_REQUEST
    elif status in (400, 422):
        kind = (
            KIND_CONTEXT_OVERFLOW
            if any(m in low for m in _OVERFLOW_MARKERS)
            else KIND_BAD_REQUEST
        )
    elif status is not None and status >= 500:
        kind = KIND_SERVER
    elif "Timeout" in type_name:
        kind = KIND_TIMEOUT
    elif "Connection" in type_name or "Connect" in type_name:
        kind = KIND_CONNECTION
    elif status is None:
        # 无 HTTP 状态：可能是流式循环里抛出的 APIError（HTTP 200 已建流），
        # 也可能是任意普通异常；必须区分，否则 RuntimeError 会被误报成「流式中断」。
        if any(m in low for m in _OVERFLOW_MARKERS):
            kind = KIND_CONTEXT_OVERFLOW
        elif _is_sdk_api_error(exc, type_name, message):
            kind = KIND_REFUSAL if _looks_like_refusal(message) else KIND_STREAM_ERROR
        else:
            kind = KIND_UNKNOWN

    if code and status and kind in (KIND_SERVER, KIND_RATE_LIMIT, KIND_QUOTA):
        message = f"[{code}] {message}" if code not in message else message

    return LLMErrorInfo(
        kind=kind,
        label=_KIND_LABELS.get(kind, kind),
        message=message,
        http_status=status,
        error_code=code,
        provider=provider,
        model=model,
        exception_type=type_name,
        retryable=kind in _RETRYABLE,
        hint=_OPERATOR_HINTS.get(kind, ""),
        request_id=str(req_id) if req_id else None,
    )


def classify_from_fields(
    *,
    status_code: Optional[int] = None,
    message: str = "",
    error_code: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    exception_type: str = "RemoteError",
) -> LLMErrorInfo:
    """IPC / 跨进程场景：只有状态码与文本时也能分类。"""

    class _RemoteError(Exception):
        def __init__(self) -> None:
            super().__init__(message or "")
            self.status_code = status_code

    _RemoteError.__name__ = exception_type
    err = _RemoteError()
    info = classify_llm_error(err, provider=provider, model=model)
    if error_code:
        info.error_code = error_code
    return info
