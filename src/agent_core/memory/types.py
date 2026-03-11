"""
记忆系统共享数据类型
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SessionSummary:
    """一次会话的摘要，构成短期记忆条目。"""

    session_id: str
    time_start: str
    time_end: str
    summary: str
    decisions: List[str] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)
    referenced_files: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    turn_count: int = 0
    token_usage: Optional[Dict[str, int]] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "session_id": self.session_id,
            "time_start": self.time_start,
            "time_end": self.time_end,
            "summary": self.summary,
            "decisions": self.decisions,
            "open_questions": self.open_questions,
            "referenced_files": self.referenced_files,
            "tags": self.tags,
            "turn_count": self.turn_count,
        }
        if self.token_usage:
            d["token_usage"] = self.token_usage
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SessionSummary":
        return cls(
            session_id=d["session_id"],
            time_start=d["time_start"],
            time_end=d["time_end"],
            summary=d["summary"],
            decisions=d.get("decisions", []),
            open_questions=d.get("open_questions", []),
            referenced_files=d.get("referenced_files", []),
            tags=d.get("tags", []),
            turn_count=d.get("turn_count", 0),
            token_usage=d.get("token_usage"),
        )

    def to_markdown(self) -> str:
        """转为 Markdown 格式，用于写入长期记忆或 QMD 索引。"""
        lines = [
            f"# Session: {self.session_id}",
            f"**时间**: {self.time_start} ~ {self.time_end}",
            "",
            "## 摘要",
            self.summary,
        ]
        if self.decisions:
            lines.append("")
            lines.append("## 关键决策")
            for d in self.decisions:
                lines.append(f"- {d}")
        if self.open_questions:
            lines.append("")
            lines.append("## 待解决问题")
            for q in self.open_questions:
                lines.append(f"- {q}")
        if self.referenced_files:
            lines.append("")
            lines.append("## 涉及文件")
            for f in self.referenced_files:
                lines.append(f"- `{f}`")
        if self.tags:
            lines.append("")
            lines.append(f"**标签**: {', '.join(self.tags)}")
        return "\n".join(lines)


@dataclass
class MemoryEntry:
    """长期记忆条目。"""

    id: str
    created_at: str
    source_session_ids: List[str] = field(default_factory=list)
    content: str = ""
    category: str = "general"
    tags: List[str] = field(default_factory=list)
    confidence: float = 1.0
    owner_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "source_session_ids": self.source_session_ids,
            "content": self.content,
            "category": self.category,
            "tags": self.tags,
            "confidence": self.confidence,
            "owner_id": self.owner_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MemoryEntry":
        return cls(
            id=d["id"],
            created_at=d["created_at"],
            source_session_ids=d.get("source_session_ids", []),
            content=d.get("content", ""),
            category=d.get("category", "general"),
            tags=d.get("tags", []),
            confidence=d.get("confidence", 1.0),
            owner_id=d.get("owner_id"),
        )

    def to_markdown(self) -> str:
        lines = [
            f"# {self.category}: {self.id}",
            f"**创建时间**: {self.created_at}",
            f"**来源会话**: {', '.join(self.source_session_ids)}",
            "",
            self.content,
        ]
        if self.tags:
            lines.append("")
            lines.append(f"**标签**: {', '.join(self.tags)}")
        return "\n".join(lines)
