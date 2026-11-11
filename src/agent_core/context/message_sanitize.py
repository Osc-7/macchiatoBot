"""Shared message normalization for LLM-bound payloads.

Two different paths currently build requests:
- ``InternalLoader.assemble`` for the live conversation (keeps multimodal blocks)
- summary paths (evict / context compression) which only need a text transcript

OpenAI-compatible APIs reject ``role=tool`` messages unless they directly answer
an assistant ``tool_calls`` block with a matching id.  Keep the invariant in one
place instead of relying on each caller to remember it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, List, Sequence, Tuple

_SYNTHESIZED_TOOL_RESULT = "（该工具调用被中断或未在当轮执行完毕，无有效输出。）"

# Fields used internally by AgentCore / providers and not needed for summarization.
_INTERNAL_MESSAGE_FIELDS = {
    "_turn_id",
    "anthropic_message_content",
    "gemini_message_content",
    "responses_reasoning_items",
    "reasoning_content",
}
_INTERNAL_TOOL_CALL_FIELDS = {"extra_content"}

_MEDIA_LABELS = {
    "image": "图片",
    "image_url": "图片",
    "input_image": "图片",
    "video": "视频",
    "video_url": "视频",
    "input_video": "视频",
    "audio": "音频",
    "input_audio": "音频",
    "file": "附件",
    "input_file": "附件",
    "media_ref": "媒体",
}

_STAT_KEYS = (
    "dropped_orphan_tool_messages",
    "synthesized_tool_results",
    "dropped_invalid_tool_calls",
    "dropped_invalid_messages",
    "flattened_content_blocks",
)


def _empty_stats() -> Dict[str, int]:
    return defaultdict(int)


def _finalize_stats(stats: Dict[str, int]) -> Dict[str, int]:
    return {key: int(stats.get(key, 0) or 0) for key in _STAT_KEYS}


def _normalize_tool_calls(
    raw_calls: Any,
    stats: Dict[str, int],
) -> List[Dict[str, Any]]:
    if not isinstance(raw_calls, list):
        return []
    out: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for tc in raw_calls:
        if not isinstance(tc, dict):
            stats["dropped_invalid_tool_calls"] += 1
            continue
        tid = tc.get("id")
        if tid is None or not str(tid).strip():
            stats["dropped_invalid_tool_calls"] += 1
            continue
        tid = str(tid).strip()
        if tid in seen_ids:
            stats["dropped_invalid_tool_calls"] += 1
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            stats["dropped_invalid_tool_calls"] += 1
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name.strip():
            stats["dropped_invalid_tool_calls"] += 1
            continue
        args = fn.get("arguments", "")
        if not isinstance(args, str):
            try:
                args = json.dumps(args if args is not None else {}, ensure_ascii=False)
            except Exception:
                args = "{}"
        normalized = dict(tc)
        normalized["id"] = tid
        normalized["type"] = str(tc.get("type") or "function")
        normalized["function"] = {
            **fn,
            "name": name,
            "arguments": args,
        }
        out.append(normalized)
        seen_ids.add(tid)
    return out


def normalize_tool_sequence(
    messages: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Return a structurally valid copy of ``messages`` plus counters.

    Rules:
    - outside an assistant tool-call block, standalone ``role=tool`` is dropped
    - tool results are matched by ``tool_call_id``, not merely by adjacency
    - duplicate/unmatched results are dropped; missing results are synthesized
    - invalid assistant tool_calls are dropped; duplicate call ids are collapsed

    The input list and dicts are never mutated.
    """
    stats = _empty_stats()
    clean: List[Dict[str, Any]] = []
    i = 0
    total = len(messages)
    while i < total:
        raw = messages[i]
        if not isinstance(raw, dict):
            stats["dropped_invalid_messages"] += 1
            i += 1
            continue

        role = raw.get("role")
        if role == "tool":
            stats["dropped_orphan_tool_messages"] += 1
            i += 1
            continue

        msg = dict(raw)
        if role != "assistant":
            # tool_calls on non-assistant messages is meaningless and can make
            # strict OpenAI-compatible endpoints reject the payload.
            if msg.get("tool_calls") is not None:
                msg.pop("tool_calls", None)
                stats["dropped_invalid_tool_calls"] += 1
            clean.append(msg)
            i += 1
            continue

        raw_calls = msg.get("tool_calls")
        if not raw_calls:
            if raw_calls is not None:
                msg.pop("tool_calls", None)
            clean.append(msg)
            i += 1
            continue

        valid_calls = _normalize_tool_calls(raw_calls, stats)
        if not valid_calls:
            msg.pop("tool_calls", None)
            clean.append(msg)
            i += 1
            continue

        msg["tool_calls"] = valid_calls
        clean.append(msg)
        expected = [str(tc["id"]) for tc in valid_calls]
        expected_set = set(expected)

        # Collect the contiguous tool-result block after this assistant message.
        by_id: Dict[str, Dict[str, Any]] = {}
        j = i + 1
        while j < total:
            candidate = messages[j]
            if not isinstance(candidate, dict) or candidate.get("role") != "tool":
                break
            tid_raw = candidate.get("tool_call_id")
            tid = str(tid_raw).strip() if tid_raw is not None else ""
            if not tid or tid not in expected_set or tid in by_id:
                stats["dropped_orphan_tool_messages"] += 1
            else:
                by_id[tid] = dict(candidate)
            j += 1

        # Emit results in tool_calls order and synthesize missing ones.
        for tid in expected:
            result = by_id.get(tid)
            if result is None:
                result = {
                    "role": "tool",
                    "tool_call_id": tid,
                    "content": _SYNTHESIZED_TOOL_RESULT,
                }
                stats["synthesized_tool_results"] += 1
            clean.append(result)
        i = j

    return clean, _finalize_stats(stats)


def _media_placeholder(block: Dict[str, Any], fallback: str) -> str:
    label = _MEDIA_LABELS.get(str(block.get("type") or ""), fallback)
    name = (
        block.get("name")
        or block.get("filename")
        or block.get("file_name")
        or ""
    )
    return f"[{label}:{name}]" if name else f"[{label}]"


def _flatten_content(content: Any, stats: Dict[str, int]) -> Any:
    """Convert multimodal content lists to plain text for summary prompts."""
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        stats["flattened_content_blocks"] += len(content)
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                text = block
            elif isinstance(block, dict):
                btype = str(block.get("type") or "")
                if btype in {"text", "input_text", "output_text"}:
                    text = str(block.get("text") or block.get("content") or "")
                elif btype in _MEDIA_LABELS:
                    text = _media_placeholder(block, "媒体")
                else:
                    value = block.get("text")
                    text = str(value) if value is not None else f"[{btype or '内容'}]"
            else:
                text = str(block)
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        value = content.get("text")
        if value is not None:
            return str(value)
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)
    return str(content)


def prepare_messages_for_summary(
    messages: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Sanitize and textify messages for summarization.

    Summaries do not continue the chat, so tool protocol and multimodal blocks
    are unnecessary.  Keep the structural repair from ``normalize_tool_sequence``
    for readable tool context, then flatten content lists and drop provider
    bookkeeping fields.
    """
    normalized, stats = normalize_tool_sequence(messages)
    out: List[Dict[str, Any]] = []
    for raw in normalized:
        msg = dict(raw)
        for key in _INTERNAL_MESSAGE_FIELDS:
            msg.pop(key, None)
        if "content" in msg:
            msg["content"] = _flatten_content(msg.get("content"), stats)
        calls = msg.get("tool_calls")
        if isinstance(calls, list):
            cleaned_calls: List[Dict[str, Any]] = []
            for tc in calls:
                if not isinstance(tc, dict):
                    continue
                tc = dict(tc)
                for key in _INTERNAL_TOOL_CALL_FIELDS:
                    tc.pop(key, None)
                cleaned_calls.append(tc)
            msg["tool_calls"] = cleaned_calls
        out.append(msg)
    return out, _finalize_stats(stats)


def stats_are_nonzero(stats: Dict[str, int]) -> bool:
    return any(int(v or 0) for v in stats.values())
