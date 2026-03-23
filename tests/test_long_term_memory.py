from __future__ import annotations

from agent_core.memory.long_term import LongTermMemory


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
