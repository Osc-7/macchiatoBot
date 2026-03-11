"""
短期记忆 - 最近 K 个会话摘要队列（当前未接入主流程）

说明：
- 提供基于 JSONL 的 SessionSummary FIFO 队列实现
- 现有 Agent 记忆架构已采用 WorkingMemory + 会话级 SessionSummarizer 直接写入长期记忆
- ShortTermMemory 暂未在运行时使用，仅作为未来扩展/实验用途保留
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from .types import SessionSummary


class ShortTermMemory:
    """最近 K 个会话摘要的 FIFO 队列，持久化到 JSONL 文件。"""

    def __init__(self, storage_dir: str, k: int = 20):
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._k = k
        self._file = self._dir / "sessions.jsonl"
        self._entries: List[SessionSummary] = self._load()

    def _load(self) -> List[SessionSummary]:
        if not self._file.exists():
            return []
        entries: List[SessionSummary] = []
        with open(self._file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(SessionSummary.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError):
                    continue
        return entries

    def _save(self) -> None:
        with open(self._file, "w", encoding="utf-8") as f:
            for entry in self._entries:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    @property
    def entries(self) -> List[SessionSummary]:
        return list(self._entries)

    @property
    def count(self) -> int:
        return len(self._entries)

    def add(self, summary: SessionSummary) -> List[SessionSummary]:
        """
        添加新会话摘要。若超过 K 容量，返回被出队的旧条目。

        Returns:
            被出队的旧 SessionSummary 列表（需触发长期记忆提炼）
        """
        self._entries.append(summary)
        evicted: List[SessionSummary] = []
        while len(self._entries) > self._k:
            evicted.append(self._entries.pop(0))
        self._save()
        return evicted

    def get_recent(self, n: Optional[int] = None) -> List[SessionSummary]:
        """获取最近 n 条（默认全部）。"""
        if n is None:
            return list(self._entries)
        return list(self._entries[-n:])

    def search(self, query: str, top_n: int = 5) -> List[SessionSummary]:
        """基于关键词的简单搜索（BM25 之前的 fallback）。"""
        query_lower = query.lower()
        scored: List[tuple] = []
        for entry in self._entries:
            text = f"{entry.summary} {' '.join(entry.tags)} {' '.join(entry.decisions)}"
            score = sum(1 for word in query_lower.split() if word in text.lower())
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:top_n]]

    def to_context_string(self, max_entries: int = 5) -> str:
        """将最近条目格式化为可注入 system prompt 的文本。"""
        recent = self.get_recent(max_entries)
        if not recent:
            return ""
        parts = ["## 最近会话记忆"]
        for entry in reversed(recent):
            parts.append(f"\n### {entry.session_id} ({entry.time_start})")
            parts.append(entry.summary)
            if entry.decisions:
                parts.append("决策: " + "; ".join(entry.decisions))
        return "\n".join(parts)
