"""
CoreLifecycleLogger — 以 Core 生命周期为单位的轻量日志。

设计目标：
- 以 Kernel/CorePool 为中心记录日志，而非前端/CLI 维度
- 每个 Core（AgentCore 实例）一个独立的 JSONL 文件
- 命名方式与记忆库 owner 一致：session-{source}:{user_id}-YYYYmmdd_HHMMSS.jsonl

记录范围：
- core_start: 创建 Core 时记录 source/user_id/session_id/profile.mode
- turn_start / turn_end: 每次请求的用户输入与最终输出
- llm_request / llm_response: LLM 调用请求和响应摘要
- tool_call / tool_result: 工具调用入参与结果
- core_end: evict/kill 时记录 token 用量等摘要（CoreStatsAction）

同时实现 SessionLogger 的鸭子类型接口，可直接赋给 AgentCore._session_logger。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Dict, List, Optional


def _safe_ns(value: str, default: str) -> str:
    return (value or "").strip() or default


@dataclass
class CoreLifecycleLogger:
    base_dir: str
    source: str
    user_id: str
    session_id: str

    _file_path: Optional[Path] = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)
    _file: Optional[IO[str]] = field(default=None, repr=False)
    enable_detailed_log: bool = field(default=False, repr=False)
    max_system_prompt_log_len: int = field(default=0, repr=False)
    _current_request_id: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        base = Path(self.base_dir or "./logs/sessions")
        base.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        src = _safe_ns(self.source, "cli")

        # 文件名采用 session-{source}:{user_id}-*.jsonl，user_id 仅用于区分 owner，
        # 具体 session_id 仍在记录体内单独字段展示。
        uid = _safe_ns(self.user_id, "root")
        filename = f"session-{src}:{uid}-{ts}.jsonl"
        self._file_path = base / filename
        # 保持文件句柄常开，避免每次 _write() 都 open/close 阻塞 event loop
        try:
            self._file = open(self._file_path, "a", encoding="utf-8")
        except Exception:
            self._file = None

    def _timestamp(self) -> str:
        dt = datetime.now().astimezone()
        return dt.isoformat(timespec="milliseconds")

    def _write(self, record: Dict[str, Any]) -> None:
        if self._closed or self._file is None:
            return
        if self._current_request_id and "request_id" not in record:
            record = {**record, "request_id": self._current_request_id}
        try:
            line = json.dumps(record, ensure_ascii=False) + "\n"
            self._file.write(line)
            self._file.flush()
        except Exception:
            pass

    # ---- 事件接口 ---------------------------------------------------------

    def on_core_start(self, *, profile: Any | None = None) -> None:
        mode = getattr(profile, "mode", None) if profile is not None else None
        self._write(
            {
                "event": "core_start",
                "timestamp": self._timestamp(),
                "session_id": self.session_id,
                "source": self.source,
                "user_id": self.user_id,
                "mode": mode,
            }
        )

    def on_turn_start(
        self, turn_id: int, text: str, *, request_id: str = ""
    ) -> None:
        if request_id:
            self._current_request_id = request_id
        self._write(
            {
                "event": "turn_start",
                "timestamp": self._timestamp(),
                "session_id": self.session_id,
                "turn_id": turn_id,
                "input": text,
                **({"request_id": request_id} if request_id else {}),
            }
        )

    def on_turn_end(
        self,
        turn_id: int,
        *,
        output_text: str,
        metadata: Dict[str, Any] | None = None,
        request_id: str = "",
    ) -> None:
        record: Dict[str, Any] = {
            "event": "turn_end",
            "timestamp": self._timestamp(),
            "session_id": self.session_id,
            "turn_id": turn_id,
            "output": output_text,
        }
        if request_id:
            record["request_id"] = request_id
        if metadata:
            record["metadata"] = metadata
        self._write(record)

    # ---- SessionLogger 鸭子类型接口（供 AgentCore._session_logger 使用）----

    def on_user_message(self, turn_id: int, content: str) -> None:
        self._write(
            {
                "event": "user_message",
                "timestamp": self._timestamp(),
                "session_id": self.session_id,
                "turn_id": turn_id,
                "content": content[:2000] + ("..." if len(content) > 2000 else ""),
            }
        )

    def on_llm_request(
        self,
        turn_id: int,
        iteration: int,
        message_count: int,
        tool_count: int,
        system_prompt_len: int = 0,
        system_prompt: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        record: Dict[str, Any] = {
            "event": "llm_request",
            "timestamp": self._timestamp(),
            "session_id": self.session_id,
            "turn_id": turn_id,
            "iteration": iteration,
            "message_count": message_count,
            "tool_count": tool_count,
            "system_prompt_len": system_prompt_len,
        }
        if self.enable_detailed_log:
            if system_prompt is not None:
                max_len = self.max_system_prompt_log_len or -1
                if max_len >= 0 and len(system_prompt) > max_len:
                    record["system_prompt"] = system_prompt[:max_len] + "..."
                else:
                    record["system_prompt"] = system_prompt
            if messages is not None:
                record["messages"] = messages
        self._write(record)

    def on_llm_response(
        self,
        turn_id: int,
        iteration: int,
        response: Any,
    ) -> None:
        tool_calls: List[Dict[str, Any]] = []
        for tc in getattr(response, "tool_calls", []) or []:
            args = getattr(tc, "arguments", None)
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw_preview": args[:500] + ("...(截断)" if len(args) > 500 else "")}
            tool_calls.append(
                {
                    "id": getattr(tc, "id", ""),
                    "name": getattr(tc, "name", ""),
                    "arguments": args,
                }
            )
        content = getattr(response, "content", None) or ""
        record: Dict[str, Any] = {
            "event": "llm_response",
            "timestamp": self._timestamp(),
            "session_id": self.session_id,
            "turn_id": turn_id,
            "iteration": iteration,
            "content_preview": content[:500] + ("..." if len(content) > 500 else ""),
            "tool_calls": tool_calls,
            "finish_reason": getattr(response, "finish_reason", None),
        }
        usage = getattr(response, "usage", None)
        if usage:
            uhit = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
            umiss = getattr(usage, "prompt_cache_miss_tokens", 0) or 0
            record["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
                "prompt_cache_hit_tokens": int(uhit),
                "prompt_cache_miss_tokens": int(umiss),
            }
        self._write(record)

    def on_tool_call(
        self,
        turn_id: int,
        iteration: int,
        tool_call: Any,
    ) -> None:
        args = getattr(tool_call, "arguments", None)
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except (json.JSONDecodeError, TypeError):
                args = {"_raw_preview": args[:500] + ("...(截断)" if len(args) > 500 else "")}
        self._write(
            {
                "event": "tool_call",
                "timestamp": self._timestamp(),
                "session_id": self.session_id,
                "turn_id": turn_id,
                "iteration": iteration,
                "tool_call_id": getattr(tool_call, "id", ""),
                "name": getattr(tool_call, "name", ""),
                "arguments": args,
            }
        )

    def on_tool_result(
        self,
        turn_id: int,
        iteration: int,
        tool_call_id: str,
        result: Any,
        duration_ms: int,
    ) -> None:
        message = getattr(result, "message", "")
        data_raw = getattr(result, "data", None)
        data_str = ""
        if data_raw is not None:
            try:
                data_str = json.dumps(data_raw, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                data_str = str(data_raw)
        self._write(
            {
                "event": "tool_result",
                "timestamp": self._timestamp(),
                "session_id": self.session_id,
                "turn_id": turn_id,
                "iteration": iteration,
                "tool_call_id": tool_call_id,
                "success": getattr(result, "success", None),
                "message": message,
                "data_preview": data_str[:500] + ("..." if len(data_str) > 500 else ""),
                "error": getattr(result, "error", None),
                "duration_ms": duration_ms,
            }
        )

    def on_assistant_message(self, turn_id: int, content: str) -> None:
        self._write(
            {
                "event": "assistant_message",
                "timestamp": self._timestamp(),
                "session_id": self.session_id,
                "turn_id": turn_id,
                "content": content,
            }
        )

    # ---- Core 生命周期事件 ---------------------------------------------------

    def on_core_end(self, *, stats: Any | None = None) -> None:
        payload: Dict[str, Any] = {
            "event": "core_end",
            "timestamp": self._timestamp(),
            "session_id": self.session_id,
        }
        if stats is not None:
            # CoreStatsAction 兼容提取
            token_usage = getattr(stats, "token_usage", None)
            turn_count = getattr(stats, "turn_count", None)
            payload["token_usage"] = token_usage
            payload["turn_count"] = turn_count
        self._write(payload)
        self.close()

    def close(self) -> None:
        self._closed = True
        if self._file is not None:
            try:
                self._file.flush()
                self._file.close()
            except Exception:
                pass
            self._file = None

    def __del__(self) -> None:
        """进程崩溃或对象被意外丢弃时的最后防线，确保文件句柄不泄漏。"""
        if not self._closed:
            self.close()

    @property
    def file_path(self) -> Optional[Path]:
        return self._file_path
