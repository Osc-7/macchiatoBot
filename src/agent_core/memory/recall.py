"""
记忆检索策略（Recall Policy）

在 Agent 处理用户输入前，根据策略检索相关记忆以 enrich context。
成体系文档（MEMORY.md 等）由 prompt 直接注入；此处仅检索语料库。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from .memory_corpus import MemoryCorpus


@dataclass
class RecallResult:
    """记忆检索结果。"""

    hits: List[dict] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.hits

    @property
    def long_term(self) -> list:
        """兼容旧测试/调用方。"""
        return []

    @property
    def content(self) -> list:
        """兼容旧测试/调用方。"""
        return [(h.get("path", ""), h.get("snippet", "")) for h in self.hits]

    def to_context_string(self) -> str:
        if not self.hits:
            return ""
        parts = ["## 相关记忆检索"]
        for hit in self.hits:
            path = hit.get("path", "")
            snippet = hit.get("snippet", "")
            parts.append(f"- {path}: {snippet[:150]}")
        return "\n".join(parts)


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
        if self._force_recall:
            return True
        return any(p.search(user_input) for p in _FORCE_RECALL_PATTERNS)

    def recall(
        self,
        query: str,
        corpus: "MemoryCorpus | None" = None,
        *,
        long_term_memory=None,
        content_memory=None,
    ) -> RecallResult:
        """
        执行记忆语料库检索。

        long_term_memory / content_memory 参数已废弃，保留仅为兼容。
        """
        _ = long_term_memory
        _ = content_memory
        result = RecallResult()
        if corpus is None:
            return result
        result.hits = corpus.search(query, self._top_n)
        return result
