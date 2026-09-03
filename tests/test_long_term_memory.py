from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from agent_core.memory.long_term import LongTermMemory
from agent_core.memory.types import SessionSummary


def test_long_term_memory_recent_topic_syncs_to_corpus(tmp_path):
    mem_dir = tmp_path / "long_term"
    corpus_dir = tmp_path / "corpus"
    memory_md = tmp_path / "MEMORY.md"

    ltm = LongTermMemory(
        str(mem_dir),
        str(memory_md),
        corpus_dir=str(corpus_dir),
    )
    ltm.add_recent_topic("讨论了向量数据库", session_id="sess-1")

    md_files = list(corpus_dir.rglob("*.md"))
    assert md_files
    assert any("向量数据库" in p.read_text(encoding="utf-8") for p in md_files)


def test_long_term_memory_recent_topic_appends_across_instances(tmp_path):
    mem_dir = tmp_path / "long_term"
    memory_md = tmp_path / "MEMORY.md"

    m1 = LongTermMemory(str(mem_dir), str(memory_md))
    m2 = LongTermMemory(str(mem_dir), str(memory_md))

    m1.add_recent_topic("first", session_id="cli:root")
    m2.add_recent_topic("second", session_id="cli:test")

    m3 = LongTermMemory(str(mem_dir), str(memory_md))
    topics = m3.get_recent_topics(10)
    contents = [t.content for t in topics]
    assert "first" in contents
    assert "second" in contents


@pytest.mark.asyncio
async def test_long_term_distill_syncs_entries_to_corpus(tmp_path):
    mem_dir = tmp_path / "long_term"
    corpus_dir = tmp_path / "corpus"
    memory_md = tmp_path / "MEMORY.md"

    ltm = LongTermMemory(
        str(mem_dir),
        str(memory_md),
        corpus_dir=str(corpus_dir),
    )
    summaries = [
        SessionSummary(
            session_id="s1",
            time_start="t1",
            time_end="t2",
            summary="用户偏好使用深色模式",
        )
    ]
    llm = AsyncMock()
    llm.chat.return_value = type(
        "R",
        (),
        {
            "content": json.dumps(
                {
                    "entries": [
                        {
                            "content": "用户偏好深色模式界面",
                            "category": "preference",
                            "tags": ["ui"],
                            "confidence": 0.9,
                        }
                    ]
                }
            )
        },
    )()

    entries = await ltm.distill(summaries, llm)
    assert len(entries) == 1

    md_files = list(corpus_dir.rglob("*.md"))
    assert md_files
    assert any("深色模式" in p.read_text(encoding="utf-8") for p in md_files)


def test_base_session_id_strips_numeric_suffix():
    assert (
        LongTermMemory.base_session_id("feishu:user:ou_abc:1783431517")
        == "feishu:user:ou_abc"
    )
    assert LongTermMemory.base_session_id("cli:root") == "cli:root"
    assert (
        LongTermMemory.base_session_id("cron:job-x:2026-08-21")
        == "cron:job-x:2026-08-21"
    )


def test_merge_oldest_chapters_merges_when_over_cap(tmp_path):
    mem_dir = tmp_path / "long_term"
    corpus_dir = tmp_path / "corpus"
    memory_md = tmp_path / "MEMORY.md"

    ltm = LongTermMemory(
        str(mem_dir), str(memory_md), corpus_dir=str(corpus_dir), qmd_enabled=False
    )
    sid = "feishu:user:ou_abc:111"
    ids = []
    for i in range(10):
        e = ltm.add_recent_topic(
            f"章节{i}: 推进了工作{i}", session_id=sid, tags=["chapter", f"round:{i}"]
        )
        ids.append(e.id)

    merged = ltm.merge_oldest_chapters(
        "feishu:user:ou_abc", max_chapters=8, merge_batch=4
    )
    assert merged is not None

    chapters = [
        e for e in ltm.entries if e.category == "recent_topic" and "chapter" in e.tags
    ]
    # 10 - 4（被合并）+ 1（合并产物）
    assert len(chapters) == 7
    assert "merged" in merged.tags
    assert "（早期章节合并）" in merged.content
    assert "章节0" in merged.content and "章节3" in merged.content
    assert "章节4" not in merged.content

    # 被合并条目的镜像已删除，合并条目镜像已写（long_term 与 corpus 两处）
    for old_id in ids[:4]:
        assert not (mem_dir / "markdown" / f"{old_id}.md").exists()
        assert not (corpus_dir / "recent_topic" / f"{old_id}.md").exists()
    assert (mem_dir / "markdown" / f"{merged.id}.md").exists()
    assert (corpus_dir / "recent_topic" / f"{merged.id}.md").exists()

    # 未超限时不触发
    assert (
        ltm.merge_oldest_chapters(
            "feishu:user:ou_abc", max_chapters=8, merge_batch=4
        )
        is None
    )

    # 其他 base session 的章节不参与计数
    ltm.add_recent_topic("别的会话", session_id="cli:root", tags=["chapter"])
    assert (
        ltm.merge_oldest_chapters(
            "feishu:user:ou_abc", max_chapters=8, merge_batch=4
        )
        is None
    )
    # cli:root 自己也没超限
    assert (
        ltm.merge_oldest_chapters("cli:root", max_chapters=8, merge_batch=4) is None
    )


def test_merge_oldest_chapters_ignores_non_chapter_entries(tmp_path):
    mem_dir = tmp_path / "long_term"
    memory_md = tmp_path / "MEMORY.md"

    ltm = LongTermMemory(str(mem_dir), str(memory_md), qmd_enabled=False)
    sid = "feishu:user:ou_abc:222"
    # 无 chapter 标签的条目（如 evict 兜底写的 session-tail）不参与章节限格
    for i in range(10):
        ltm.add_recent_topic(f"普通条目{i}", session_id=sid)
    assert (
        ltm.merge_oldest_chapters(
            "feishu:user:ou_abc", max_chapters=8, merge_batch=4
        )
        is None
    )
