"""
CoreLifecycleLogger — 以 Core 生命周期为单位的轻量日志。

设计目标：
- 以 Kernel/CorePool 为中心记录日志，而非前端/CLI 维度
- 每个 Core（ScheduleAgent 实例）一个独立的 JSONL 文件
- 命名方式与记忆库 owner 一致：session-{source}:{user_id}-YYYYmmdd_HHMMSS.jsonl

记录范围（精简版）：
- core_start: 创建 Core 时记录 source/user_id/session_id/profile.mode
- turn: 每次请求的用户输入与最终输出（不记录完整 system prompt）
- core_end: evict/kill 时记录 token 用量等摘要（CoreStatsAction）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


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

    def _timestamp(self) -> str:
        dt = datetime.now().astimezone()
        return dt.isoformat(timespec="milliseconds")

    def _write(self, record: Dict[str, Any]) -> None:
        if self._closed or self._file_path is None:
            return
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(self._file_path, "a", encoding="utf-8") as f:
            f.write(line)

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

    def on_turn_start(self, turn_id: int, text: str) -> None:
        self._write(
            {
                "event": "turn_start",
                "timestamp": self._timestamp(),
                "session_id": self.session_id,
                "turn_id": turn_id,
                "input": text,
            }
        )

    def on_turn_end(self, turn_id: int, *, output_text: str, metadata: Dict[str, Any] | None = None) -> None:
        record: Dict[str, Any] = {
            "event": "turn_end",
            "timestamp": self._timestamp(),
            "session_id": self.session_id,
            "turn_id": turn_id,
            "output": output_text,
        }
        if metadata:
            record["metadata"] = metadata
        self._write(record)

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

    @property
    def file_path(self) -> Optional[Path]:
        return self._file_path

