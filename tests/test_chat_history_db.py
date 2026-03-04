from __future__ import annotations

import sqlite3

from schedule_agent.core.memory.chat_history_db import ChatHistoryDB


def test_chat_history_db_filters_by_default_source(tmp_path):
    db_path = tmp_path / "chat_history.db"
    db = ChatHistoryDB(str(db_path))
    db.write_message(session_id="s1", role="user", content="hello from cli", source="cli")
    db.write_message(session_id="s2", role="user", content="hello from qq", source="qq")
    db.close()

    cli_db = ChatHistoryDB(str(db_path), default_source="cli")
    results = cli_db.search("hello", top_k=10)
    cli_db.close()

    assert len(results) == 1
    assert results[0]["source"] == "cli"
    assert results[0]["session_id"] == "s1"


def test_chat_history_db_migrates_legacy_schema(tmp_path):
    db_path = tmp_path / "legacy_chat.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_name TEXT,
            is_truncated INTEGER DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()

    db = ChatHistoryDB(str(db_path), default_source="cli")
    db.write_message(session_id="legacy", role="assistant", content="migrated ok")
    rows = db.get_session_messages("legacy")
    db.close()

    assert len(rows) == 1
    assert rows[0]["source"] == "cli"


def test_chat_history_db_get_session_messages_after_with_role_filter(tmp_path):
    db_path = tmp_path / "chat_history.db"
    db = ChatHistoryDB(str(db_path), default_source="cli")
    id1 = db.write_message(session_id="s1", role="user", content="u1", source="cli")
    db.write_message(session_id="s1", role="tool", content="t1", source="cli")
    db.write_message(session_id="s1", role="assistant", content="a1", source="cli")
    db.write_message(session_id="s1", role="user", content="u2", source="cli")

    rows = db.get_session_messages_after("s1", id1, roles=["user", "assistant"])
    db.close()

    assert [r["role"] for r in rows] == ["assistant", "user"]
    assert [r["content"] for r in rows] == ["a1", "u2"]
