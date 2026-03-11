"""
记忆检索策略（Recall Policy）

在 Agent 处理用户输入前，根据策略检索相关记忆以 enrich context。
仅检索长期记忆和内容记忆；短期会话和 MEMORY.md 由 Agent 直接加载，无需检索。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Tuple

from .types import MemoryEntry


@dataclass
class RecallResult:
    """记忆检索结果（长期 + 内容）。"""

    long_term: List[MemoryEntry] = field(default_factory=list)
    content: List[Tuple[str, str]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.long_term and not self.content

    def to_context_string(self) -> str:
        """格式化为可注入 system prompt 的文本。"""
        parts: List[str] = []

        if self.long_term:
            parts.append("## 长期经验")
            for e in self.long_term:
                parts.append(f"- [{e.category}] {e.content}")

        if self.content:
            parts.append("\n## 相关内容记忆")
            for path, snippet in self.content:
                parts.append(f"- {path}: {snippet[:150]}")

        return "\n".join(parts) if parts else ""


_FORCE_RECALL_PATTERNS = [
    re.compile(r"(上次|之前|以前|过去|历史|之前我们|上回)", re.IGNORECASE),
    re.compile(r"(记得|还记得|你还记得)", re.IGNORECASE),
    re.compile(r"(经验|教训|惯例|偏好|习惯)", re.IGNORECASE),
    re.compile(r"(延续|继续|接着|根据上次)", re.IGNORECASE),
    re.compile(r"(笔记|文档|讲义|会议记录)", re.IGNORECASE),
]


class RecallPolicy:
    """记忆检索策略管理器。"""

    def __init__(
        self,
        force_recall: bool = True,
        top_n: int = 5,
        score_threshold: float = 0.3,
    ):
        self._force_recall = force_recall
        self._top_n = top_n
        self._score_threshold = score_threshold

    def should_recall(self, user_input: str) -> bool:
        """判断是否需要执行记忆检索。"""
        if self._force_recall:
            return True
        return any(p.search(user_input) for p in _FORCE_RECALL_PATTERNS)

    def recall(
        self,
        query: str,
        long_term_memory=None,
        content_memory=None,
    ) -> RecallResult:
        """
        执行记忆检索（仅长期 + 内容）。短期和 MEMORY.md 由 Agent 直接加载。

        Args:
            query: 用户输入
            long_term_memory: LongTermMemory 实例
            content_memory: ContentMemory 实例

        Returns:
            RecallResult 聚合结果
        """
        result = RecallResult()

        if long_term_memory:
            # 先取略多一些结果，再按 confidence 做阈值过滤
            raw_entries = long_term_memory.search(query, self._top_n * 2)
            if self._score_threshold is not None and self._score_threshold > 0:
                filtered = [
                    e
                    for e in raw_entries
                    if getattr(e, "confidence", 1.0) >= self._score_threshold
                ]
            else:
                filtered = raw_entries
            result.long_term = filtered[: self._top_n]

        if content_memory:
            hits = content_memory.search(query, self._top_n)
            result.content = [(str(p), s) for p, s in hits]
            qmd_hits = content_memory.search_qmd(query, self._top_n)
            for hit in qmd_hits:
                path = hit.get("path", hit.get("file", "unknown"))
                snippet = hit.get("snippet", hit.get("content", ""))[:300]
                result.content.append((str(path), snippet))

        return result
