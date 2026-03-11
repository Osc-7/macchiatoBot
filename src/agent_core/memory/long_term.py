"""
长期记忆 - 提炼持久化经验知识

从短期记忆出队条目中提炼关键信息，仅写入：
1. 长期记忆 JSONL（entries.jsonl）
2. markdown 单条文件（data/memory/long_term/markdown/）
3. QMD collection（可选，语义检索）

MEMORY.md 由 Agent 通过 write_file/modify_file 自主维护，不在此自动更新。
"""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .types import MemoryEntry, SessionSummary

_QMD_COLLECTION_NAME = "agent_memory"


def _write_to_qmd_collection(
    md_path: Path,
    qmd_command: str = "qmd",
    collection_name: str = _QMD_COLLECTION_NAME,
) -> bool:
    """将 Markdown 文件所在目录注册为 QMD collection 并触发 embed（best-effort）。"""
    try:
        subprocess.run(
            [
                qmd_command,
                "collection",
                "add",
                str(md_path.parent),
                "--name",
                collection_name,
            ],
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            [qmd_command, "embed"],
            capture_output=True,
            timeout=120,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return False


_DISTILL_SYSTEM_PROMPT = """\
你是一个知识提炼引擎。给定若干条过期的会话摘要，请提取其中值得长期保留的核心信息。

输出一个 JSON 对象：
{
  "entries": [
    {
      "content": "提炼后的知识点（1-3 句话）",
      "category": "preference|decision|lesson|constraint|workflow",
      "tags": ["标签1", "标签2"],
      "confidence": 0.9
    }
  ]
}

规则：
- 只保留有长期价值的信息（偏好、约束、流程、教训、决策依据）
- 丢弃临时性、一次性信息
- confidence 范围 0-1
- 使用中文
- 只输出合法 JSON，不要包含 markdown 代码块标记"""


class LongTermMemory:
    """长期记忆管理器。"""

    def __init__(
        self,
        storage_dir: str,
        memory_md_path: str = "./MEMORY.md",
        qmd_enabled: bool = False,
        qmd_command: str = "qmd",
    ):
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._entries_file = self._dir / "entries.jsonl"
        self._memory_md = Path(memory_md_path)
        self._qmd_enabled = qmd_enabled
        self._qmd_command = qmd_command
        self._md_dir = self._dir / "markdown"
        self._md_dir.mkdir(parents=True, exist_ok=True)
        self._entries: List[MemoryEntry] = self._load()
        self._entries_mtime: float = (
            self._entries_file.stat().st_mtime if self._entries_file.exists() else 0.0
        )

    def _load(self) -> List[MemoryEntry]:
        if not self._entries_file.exists():
            return []
        entries: List[MemoryEntry] = []
        with open(self._entries_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(MemoryEntry.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError):
                    continue
        return entries

    def _save(self) -> None:
        with open(self._entries_file, "w", encoding="utf-8") as f:
            for entry in self._entries:
                f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        self._entries_mtime = (
            self._entries_file.stat().st_mtime if self._entries_file.exists() else 0.0
        )

    def _append_entry(self, entry: MemoryEntry) -> None:
        with open(self._entries_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        self._entries_mtime = (
            self._entries_file.stat().st_mtime if self._entries_file.exists() else 0.0
        )

    def _refresh_entries_if_needed(self) -> None:
        if not self._entries_file.exists():
            self._entries_mtime = 0.0
            return
        mtime = self._entries_file.stat().st_mtime
        if mtime <= self._entries_mtime:
            return
        self._entries = self._load()
        self._entries_mtime = mtime

    @property
    def entries(self) -> List[MemoryEntry]:
        self._refresh_entries_if_needed()
        return list(self._entries)

    async def distill(
        self,
        evicted_summaries: List[SessionSummary],
        llm_client,
    ) -> List[MemoryEntry]:
        """
        将出队的短期记忆提炼为长期记忆条目。

        Args:
            evicted_summaries: 被出队的会话摘要列表
            llm_client: LLMClient 实例

        Returns:
            新创建的长期记忆条目列表
        """
        if not evicted_summaries:
            return []

        input_text = "\n\n---\n\n".join(s.to_markdown() for s in evicted_summaries)
        source_ids = [s.session_id for s in evicted_summaries]

        response = await llm_client.chat(
            messages=[{"role": "user", "content": input_text}],
            system_message=_DISTILL_SYSTEM_PROMPT,
        )

        raw = (response.content or "").strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            return []

        now_str = datetime.now(timezone.utc).isoformat()
        new_entries: List[MemoryEntry] = []

        for item in result.get("entries", []):
            entry = MemoryEntry(
                id=f"mem-{uuid.uuid4().hex[:8]}",
                created_at=now_str,
                source_session_ids=source_ids,
                content=item.get("content", ""),
                category=item.get("category", "general"),
                tags=item.get("tags", []),
                confidence=item.get("confidence", 0.5),
            )
            self._entries.append(entry)
            self._append_entry(entry)
            new_entries.append(entry)

        if new_entries:
            self._write_entries_as_markdown(new_entries)

        return new_entries

    def _write_entries_as_markdown(self, entries: List[MemoryEntry]) -> None:
        """将记忆条目写为 Markdown 文件并可选地同步到 QMD。"""
        for entry in entries:
            md_path = self._md_dir / f"{entry.id}.md"
            md_path.write_text(entry.to_markdown(), encoding="utf-8")

        if self._qmd_enabled:
            _write_to_qmd_collection(self._md_dir, self._qmd_command)

    def _ensure_memory_md(self) -> None:
        """确保 MEMORY.md 存在，若不存在则创建初始模板。"""
        if self._memory_md.exists():
            return
        self._memory_md.parent.mkdir(parents=True, exist_ok=True)
        template = """\
# MEMORY.md - Agent 长期记忆

> 本文件由记忆系统自动维护，人类可手动编辑以修正或补充。
> 标记为 *(待人工确认)* 的条目尚未经人工审核。

## 用户长期偏好


## 稳定工作流约束


## 关键历史决策


## 反模式与踩坑


## 经验教训

"""
        with open(self._memory_md, "w", encoding="utf-8") as f:
            f.write(template)

    def _search_qmd(self, query: str, top_n: int) -> List[MemoryEntry]:
        """通过 QMD 语义检索长期记忆（仅命中 _md_dir 下的条目）。"""
        if not self._qmd_enabled or not self._entries:
            return []
        try:
            result = subprocess.run(
                [self._qmd_command, "query", query, "--json", "-n", str(top_n * 2)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return []
            raw = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            raw = (
                raw.get("results", raw.get("hits", [])) if isinstance(raw, dict) else []
            )
        entries_by_id = {e.id: e for e in self._entries}
        seen: set[str] = set()
        out: List[MemoryEntry] = []
        for hit in raw:
            if len(out) >= top_n:
                break
            path = hit.get("path") or hit.get("file") or ""
            eid = Path(path).stem
            if eid in seen or eid not in entries_by_id:
                continue
            seen.add(eid)
            out.append(entries_by_id[eid])
        return out

    def search(
        self,
        query: str,
        top_n: int = 5,
        owner_id: Optional[str] = None,
    ) -> List[MemoryEntry]:
        """
        搜索长期记忆：QMD 开启时优先语义检索，不足则用关键词补充。

        Args:
            query: 检索查询
            top_n: 返回条数上限
            owner_id: 可选，多用户共享时仅返回该 owner 的条目
        """
        self._refresh_entries_if_needed()
        entries = self._entries
        if owner_id is not None:
            entries = [e for e in entries if e.owner_id == owner_id]
        result: List[MemoryEntry] = []
        if self._qmd_enabled:
            result = self._search_qmd(query, top_n)
            if owner_id is not None:
                result = [e for e in result if e.owner_id == owner_id]
        if len(result) < top_n:
            query_lower = query.lower()
            scored: List[tuple] = []
            for entry in entries:
                if entry in result:
                    continue
                text = f"{entry.content} {' '.join(entry.tags)} {entry.category}"
                score = sum(1 for w in query_lower.split() if w in text.lower())
                if score > 0:
                    scored.append((score, entry))
            scored.sort(key=lambda x: x[0], reverse=True)
            seen_ids = {e.id for e in result}
            for _, entry in scored:
                if len(result) >= top_n:
                    break
                if entry.id not in seen_ids:
                    seen_ids.add(entry.id)
                    result.append(entry)
        return result

    def to_context_string(self, max_entries: int = 10) -> str:
        """将长期记忆格式化为可注入 system prompt 的文本。"""
        self._refresh_entries_if_needed()
        if not self._entries:
            return ""
        recent = self._entries[-max_entries:]
        parts = ["## 长期经验记忆"]
        for entry in recent:
            parts.append(f"- [{entry.category}] {entry.content}")
        return "\n".join(parts)

    def read_memory_md(self) -> str:
        """读取 MEMORY.md 全文。"""
        self._ensure_memory_md()
        return self._memory_md.read_text(encoding="utf-8")

    def add_recent_topic(
        self,
        summary: str,
        session_id: str,
        tags: Optional[List[str]] = None,
        owner_id: Optional[str] = None,
    ) -> MemoryEntry:
        """
        添加一条「最近话题」条目（category=recent_topic）到 entries.jsonl。

        与 distill() 不同，此方法直接存储会话摘要，不经过 LLM 提炼，
        适合在 session 切分时快速记录最近发生的事。

        Args:
            summary: 会话摘要文本
            session_id: 来源会话 ID
            tags: 可选标签列表
            owner_id: 可选，多用户共享时按 owner 区分（如水源 username）

        Returns:
            新创建的 MemoryEntry
        """
        now_str = datetime.now(timezone.utc).isoformat()
        entry = MemoryEntry(
            id=f"topic-{uuid.uuid4().hex[:8]}",
            created_at=now_str,
            source_session_ids=[session_id],
            content=summary,
            category="recent_topic",
            tags=tags or [],
            confidence=1.0,
            owner_id=owner_id,
        )
        self._entries.append(entry)
        self._append_entry(entry)

        # 同步写入 markdown 文件，并在启用时触发 QMD collection 更新。
        # 这样 recent_topic 与 distill() 生成的长期记忆在物理存储上保持一致，
        # 便于统一检索与人工查看。
        self._write_entries_as_markdown([entry])

        return entry

    def get_recent_topics(
        self,
        n: int = 10,
        owner_id: Optional[str] = None,
    ) -> List[MemoryEntry]:
        """
        获取最近 n 条 category=recent_topic 的条目，按时间从旧到新排序。

        Args:
            n: 最多返回条数
            owner_id: 可选，多用户共享时仅返回该 owner 的条目（如水源 username）

        Returns:
            最近话题条目列表
        """
        self._refresh_entries_if_needed()
        topics = [e for e in self._entries if e.category == "recent_topic"]
        if owner_id is not None:
            topics = [e for e in topics if e.owner_id == owner_id]
        return topics[-n:]
