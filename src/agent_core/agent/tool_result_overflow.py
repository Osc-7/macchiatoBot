"""
Tool result 入场截断 + 工作区落盘。

设计动机
========

LLM 单轮上下文窗口有限（128k / 200k / 1M 不等），单次工具调用可能返回远超
窗口的内容（例如 ``web_search`` 一次返回 50k+ tokens）。即使后续触发
``compress_context`` 折叠历史，按当前压缩策略也必须保留尾部完整的
``assistant(tool_calls) → tool(tool_results)`` 对（OpenAI/Anthropic 协议要求
带 tool_calls 的 assistant 后必须紧跟匹配 ``tool_call_id`` 的 tool 消息），
所以那条巨型 result 仍会原样进 prompt，照样爆窗。

本模块在 ``ConversationContext.add_tool_result`` 之前做**入场截断**：

* 估算 ``ToolResult`` 序列化后的 token 数；
* 若超阈值，将完整 JSON 落盘到工作区 ``.tool_results/{ts}_{tool}_{id}.json``，
  messages 中只保留 head 截断 + 显式标记（含相对路径），AI 可用 ``read_file``
  / ``cat`` / ``head`` / ``grep`` 按需检索；
* 否则原样返回。

落盘位置
--------

普通用户：``{workspace_owner_dir}/{overflow_dir_name}/`` —— 与 AI 的 bash
默认 cwd 一致，可用相对路径 cat。

``bash_workspace_admin`` 模式（cwd=项目根，例如 ``cli:root``）：为避免污染
项目根，转储到 ``{tmp_dir}/{overflow_dir_name}/``，并在 marker 中给绝对路径。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from agent_core.memory.working_memory import estimate_tokens
from agent_core.tools.base import ToolResult

logger = logging.getLogger(__name__)

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DATA_URL_BASE64_RE = re.compile(r"^data:([^;,]+)?;base64,", re.IGNORECASE)


@dataclass(frozen=True)
class _Base64SanitizeStats:
    """记录本次 base64 脱敏的命中统计。"""

    redacted_fields: int = 0
    redacted_chars: int = 0

    def add(self, *, fields: int = 0, chars: int = 0) -> "_Base64SanitizeStats":
        return _Base64SanitizeStats(
            redacted_fields=self.redacted_fields + int(fields),
            redacted_chars=self.redacted_chars + int(chars),
        )


def _is_base64_key(key: str) -> bool:
    """
    判断字段名是否表示 base64 负载。

    约定：
    - 显式含 ``base64``；
    - 常见文件块字段 ``file_data``。
    """
    k = (key or "").strip().lower()
    if not k:
        return False
    return ("base64" in k) or (k == "file_data")


def _is_data_url_base64(value: str) -> bool:
    """是否为 ``data:*;base64,`` 形式的数据 URL。"""
    if not isinstance(value, str):
        return False
    return bool(_DATA_URL_BASE64_RE.match(value.strip()))


def _base64_placeholder(*, chars: int, source: str) -> str:
    """构造稳定的占位文本，避免把真实 base64 放入上下文。"""
    return f"[base64 omitted: {chars} chars, source={source}]"


def _sanitize_base64_payload(obj: Any, *, parent_key: str = "") -> tuple[Any, _Base64SanitizeStats]:
    """
    递归脱敏对象中的 base64 字段，返回 (sanitized_obj, stats)。

    规则：
    - dict 中键名命中 ``_is_base64_key`` 且值为字符串：替换为占位文本；
    - 任意位置字符串若为 ``data:*;base64,``：整段替换为占位文本；
    - 其他字段递归保留。
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        stats = _Base64SanitizeStats()
        for k, v in obj.items():
            key = str(k)
            if isinstance(v, str) and _is_base64_key(key):
                out[key] = _base64_placeholder(chars=len(v), source=key)
                stats = stats.add(fields=1, chars=len(v))
                continue
            sanitized_v, child = _sanitize_base64_payload(v, parent_key=key)
            out[key] = sanitized_v
            stats = stats.add(fields=child.redacted_fields, chars=child.redacted_chars)
        return out, stats

    if isinstance(obj, list):
        out_list: list[Any] = []
        stats = _Base64SanitizeStats()
        for item in obj:
            sanitized_item, child = _sanitize_base64_payload(item, parent_key=parent_key)
            out_list.append(sanitized_item)
            stats = stats.add(fields=child.redacted_fields, chars=child.redacted_chars)
        return out_list, stats

    if isinstance(obj, str) and _is_data_url_base64(obj):
        return (
            _base64_placeholder(chars=len(obj), source=f"{parent_key or 'data_url'}"),
            _Base64SanitizeStats(redacted_fields=1, redacted_chars=len(obj)),
        )

    return obj, _Base64SanitizeStats()


def sanitize_binary_payloads(obj: Any) -> Any:
    """递归脱敏对象中的 base64 / data URL，供日志与上下文写入使用。"""
    sanitized, _ = _sanitize_base64_payload(obj)
    return sanitized


def _sanitize_tool_result_base64(result: ToolResult) -> tuple[ToolResult, _Base64SanitizeStats]:
    """
    返回用于写入上下文的 ToolResult（base64 已脱敏）。

    未命中时返回原对象，避免不必要拷贝。
    """
    sanitized_data, data_stats = _sanitize_base64_payload(result.data)
    sanitized_meta, meta_stats = _sanitize_base64_payload(result.metadata or {})
    total = data_stats.add(
        fields=meta_stats.redacted_fields, chars=meta_stats.redacted_chars
    )
    if total.redacted_fields <= 0:
        return result, total

    new_meta = dict(sanitized_meta) if isinstance(sanitized_meta, dict) else {}
    new_meta["_base64_omitted"] = {
        "fields": total.redacted_fields,
        "chars": total.redacted_chars,
    }
    return (
        ToolResult(
            success=result.success,
            data=sanitized_data,
            message=result.message,
            error=result.error,
            metadata=new_meta,
        ),
        total,
    )


def _sanitize_for_filename(value: str, *, fallback: str = "x") -> str:
    """把任意字符串规范成可安全用作文件名的片段。"""
    cleaned = _FILENAME_SAFE_RE.sub("_", (value or "").strip())
    cleaned = cleaned.strip("_.") or fallback
    return cleaned[:80]


def _truncate_string_to_tokens(text: str, target_tokens: int) -> str:
    """
    把 ``text`` 截断到估算 token 数 ≤ ``target_tokens`` 的最长前缀。

    估算口径与 ``estimate_tokens`` 一致（中文 1.5 字/token，其他 4 字符/token），
    采用「先按估算下取上界 → 二分校正」的策略，3-5 次迭代即可收敛。
    """
    if target_tokens <= 0 or not text:
        return ""
    if estimate_tokens(text) <= target_tokens:
        return text
    # 上界：按最低密度 1.5 字符/token 估算（保守偏长）
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= target_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


@dataclass(frozen=True)
class OverflowOutcome:
    """``maybe_offload_tool_result`` 的执行结果元数据，便于审计/日志。"""

    triggered: bool
    """是否实际发生了截断 + 落盘"""

    overflow_path: Optional[Path] = None
    """完整内容的转储绝对路径；未触发时为 None"""

    original_tokens: int = 0
    """原始 to_json() 的估算 token 数"""

    kept_tokens: int = 0
    """截断后 to_json() 的估算 token 数"""

    display_path: str = ""
    """marker 中展示给 AI 的路径（相对工作区或绝对）"""


def maybe_offload_tool_result(
    result: ToolResult,
    *,
    tool_name: str,
    tool_call_id: str,
    workspace_dir: str,
    max_tokens: Optional[int],
    overflow_dir_name: str = ".tool_results",
    is_workspace_admin: bool = False,
    admin_overflow_dir: Optional[str] = None,
) -> Tuple[ToolResult, OverflowOutcome]:
    """
    若 ``result`` 序列化后超过 ``max_tokens``，将完整内容落盘到工作区
    ``overflow_dir_name`` 子目录，并返回截断后的新 ``ToolResult``。

    Parameters
    ----------
    result :
        原始工具执行结果。
    tool_name, tool_call_id :
        用于生成转储文件名（清洗特殊字符）。
    workspace_dir :
        AI 的工作区目录绝对/相对路径（普通用户为
        ``{workspace_base_dir}/{frontend}/{user_id}/``）。
    max_tokens :
        触发阈值（按估算 token 数）；``None`` 或 ``<=0`` 时禁用此机制，原样返回。
    overflow_dir_name :
        转储文件相对工作区的子目录名。
    is_workspace_admin :
        若为 ``True`` 则该 Core 的 cwd 是项目根（不应污染），转储改放到
        ``admin_overflow_dir``（通常是该用户的 ``/tmp/macchiato/.../{overflow_dir_name}``）。
    admin_overflow_dir :
        管理员模式下的转储绝对/相对目录；为 ``None`` 时回退到 ``workspace_dir``。

    Returns
    -------
    (new_result, outcome)
        ``new_result``：若未触发，与入参为同一对象；触发时为新构造的
        ``ToolResult``，``data`` 仅含 head preview 与元信息，``message`` 末尾追加
        显式截断 marker。
        ``outcome``：本次操作的统计元数据。
    """
    # 无论是否启用 overflow，先做 base64 脱敏，避免把大块编码内容写入上下文。
    result_for_context, _sanitize_stats = _sanitize_tool_result_base64(result)

    if not max_tokens or max_tokens <= 0:
        return result_for_context, OverflowOutcome(triggered=False)

    try:
        original_json = result_for_context.to_json()
    except Exception as exc:  # pragma: no cover —— ToolResult.to_json 极少抛
        logger.warning("maybe_offload_tool_result: to_json failed: %s", exc)
        return result_for_context, OverflowOutcome(triggered=False)

    original_tokens = estimate_tokens(original_json)
    if original_tokens <= max_tokens:
        return result_for_context, OverflowOutcome(
            triggered=False, original_tokens=original_tokens
        )

    # ── 选择落盘目录 ────────────────────────────────────────────────────
    if is_workspace_admin and admin_overflow_dir:
        target_dir = Path(admin_overflow_dir).expanduser()
    else:
        target_dir = Path(workspace_dir).expanduser() / overflow_dir_name

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # 落盘失败时仍做截断（防爆窗优先），marker 里说明原因
        logger.warning(
            "maybe_offload_tool_result: mkdir %s failed: %s; will truncate without persistence",
            target_dir,
            exc,
        )
        return _build_truncated_result(
            result=result_for_context,
            original_json=original_json,
            original_tokens=original_tokens,
            max_tokens=max_tokens,
            display_path="",
            persist_error=str(exc),
        ), OverflowOutcome(
            triggered=True,
            overflow_path=None,
            original_tokens=original_tokens,
            kept_tokens=0,
            display_path="",
        )

    # ── 生成文件名并写盘 ────────────────────────────────────────────────
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_tool = _sanitize_for_filename(tool_name, fallback="tool")
    safe_id = _sanitize_for_filename(tool_call_id or "", fallback="noid")[:24]
    filename = f"{ts}_{safe_tool}_{safe_id}.json"
    overflow_path = target_dir / filename

    try:
        overflow_path.write_text(original_json, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "maybe_offload_tool_result: write %s failed: %s; will truncate without persistence",
            overflow_path,
            exc,
        )
        return _build_truncated_result(
            result=result_for_context,
            original_json=original_json,
            original_tokens=original_tokens,
            max_tokens=max_tokens,
            display_path="",
            persist_error=str(exc),
        ), OverflowOutcome(
            triggered=True,
            overflow_path=None,
            original_tokens=original_tokens,
            kept_tokens=0,
            display_path="",
        )

    # 给 AI 看的路径：管理员模式下用绝对路径；普通用户用相对工作区路径
    if is_workspace_admin and admin_overflow_dir:
        display_path = str(overflow_path.resolve())
    else:
        display_path = f"{overflow_dir_name}/{filename}"

    new_result = _build_truncated_result(
        result=result_for_context,
        original_json=original_json,
        original_tokens=original_tokens,
        max_tokens=max_tokens,
        display_path=display_path,
        persist_error=None,
    )
    kept_tokens = estimate_tokens(new_result.to_json())

    logger.info(
        "tool result overflow: tool=%s id=%s original=%d tokens > limit=%d; "
        "persisted=%s kept=%d tokens",
        tool_name,
        tool_call_id,
        original_tokens,
        max_tokens,
        overflow_path,
        kept_tokens,
    )

    return new_result, OverflowOutcome(
        triggered=True,
        overflow_path=overflow_path.resolve(),
        original_tokens=original_tokens,
        kept_tokens=kept_tokens,
        display_path=display_path,
    )


def _build_truncated_result(
    *,
    result: ToolResult,
    original_json: str,
    original_tokens: int,
    max_tokens: int,
    display_path: str,
    persist_error: Optional[str],
) -> ToolResult:
    """
    构造截断后的 ``ToolResult``：

    * ``success`` / ``error`` 保留；
    * ``message`` = 原 message + 显式截断标记（一行话讲清楚去哪取完整内容）；
    * ``data`` 替换为结构化 dict：``{"truncated": True, "preview": <head text>,
      "original_tokens": N, "kept_tokens": M, "overflow_path": display_path}``，
      让 AI 既能看到 head preview，又知道完整内容路径；
    * ``metadata`` 注入 ``_overflow`` 字段供审计。

    head preview 的 token 预算 = ``max_tokens - 元信息 overhead``，确保最终
    ``new_result.to_json()`` 的估算不超过 ``max_tokens``。
    """
    # 元信息（除 preview 外）的 token 占用预估，留出余量
    overhead_tokens = 200
    preview_budget = max(max_tokens - overhead_tokens, 100)

    # 取原始 JSON 的 head 作为 preview。从 data 字段直接截断更直观，但 ToolResult
    # 的序列化形态多样（data 可能是 dict / list / str / 嵌套结构），统一用
    # to_json 字符串的 head 既稳定又包含 message / 错误等关键信息。
    preview = _truncate_string_to_tokens(original_json, preview_budget)

    if display_path:
        marker = (
            f"\n\n[此工具结果原始约 {original_tokens} tokens，已截断保留前 "
            f"~{estimate_tokens(preview)} tokens。完整内容存档：{display_path}（"
            f"位于当前工作区，可用 read_file 或 cat 查看完整 JSON）]"
        )
    else:
        # 落盘失败时的退化标记
        marker = (
            f"\n\n[此工具结果原始约 {original_tokens} tokens，已截断保留前 "
            f"~{estimate_tokens(preview)} tokens；本次落盘失败"
            + (f"（{persist_error}）" if persist_error else "")
            + "，完整内容已无法检索]"
        )

    new_message = (result.message or "") + marker

    new_data = {
        "truncated": True,
        "original_tokens": original_tokens,
        "preview_tokens": estimate_tokens(preview),
        "overflow_path": display_path,
        "preview": preview,
    }
    if persist_error:
        new_data["persist_error"] = persist_error

    new_metadata = dict(result.metadata or {})
    new_metadata["_overflow"] = {
        "triggered": True,
        "original_tokens": original_tokens,
        "max_tokens": max_tokens,
        "overflow_path": display_path,
        "persist_error": persist_error,
    }

    return ToolResult(
        success=result.success,
        data=new_data,
        message=new_message,
        error=result.error,
        metadata=new_metadata,
    )


def estimate_result_tokens(result: ToolResult) -> int:
    """对外便捷：估算一个 ``ToolResult`` 序列化后的 token 数（供测试/调试）。"""
    try:
        return estimate_tokens(result.to_json())
    except Exception:
        return 0


def resolve_overflow_dirs(
    *,
    cmd_cfg: Any,
    user_id: str,
    source: str,
    profile: Any = None,
    overflow_dir_name: str = ".tool_results",
) -> Tuple[str, bool, Optional[str]]:
    """
    解析 ``maybe_offload_tool_result`` 所需的目录信息（封装与 workspace_paths 的耦合）。

    Returns
    -------
    (workspace_dir, is_workspace_admin, admin_overflow_dir)
        * ``workspace_dir``：``{workspace_base_dir}/{frontend}/{user_id}/``，
          普通用户的转储基址；
        * ``is_workspace_admin``：当前 Core 是否被视为工作区管理员；
        * ``admin_overflow_dir``：管理员模式下的转储目录（位于
          ``/tmp/macchiato/.../{overflow_dir_name}``），普通用户场景为 ``None``。
    """
    from agent_core.agent.workspace_paths import (
        is_bash_workspace_admin,
        resolve_workspace_owner_dir,
        resolve_workspace_tmp_dir,
    )

    workspace_dir = resolve_workspace_owner_dir(cmd_cfg, user_id, source=source)
    admin = is_bash_workspace_admin(cmd_cfg, source, user_id, profile)
    admin_dir: Optional[str] = None
    if admin:
        admin_dir = str(Path(resolve_workspace_tmp_dir(cmd_cfg, user_id, source=source)) / overflow_dir_name)
    return workspace_dir, admin, admin_dir
