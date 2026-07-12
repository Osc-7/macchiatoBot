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
