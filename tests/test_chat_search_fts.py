"""chat_history_db 的 FTS 查询转义与检索回归。

背景：`gpu-worker` 这类含连字符的查询以前直接拼进 MATCH，会被 FTS5 解析成
`gpu - worker`，报 `no such column: worker`。这里锁定「每个词整体加引号」的行为。
"""
from __future__ import annotations

import pytest

from agent_core.memory.chat_history_db import ChatHistoryDB, _escape_fts_query


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("gpu-worker", '"gpu-worker"'),
        ("a b", '"a" OR "b"'),
        ('a"b', '"a""b"'),
        ("   ", '""'),
        ("autoresearch 部署", '"autoresearch" OR "部署"'),
        ("C++", '"C++"'),
    ],
)
def test_escape_quotes_every_token(query: str, expected: str) -> None:
    assert _escape_fts_query(query) == expected


def test_escape_hyphen_stays_inside_one_phrase() -> None:
    escaped = _escape_fts_query("gpu-worker")
    # 没被拆成 "gpu - worker"（那正是 SQL 报错的根因）
    assert escaped == '"gpu-worker"'
    assert " - " not in escaped


@pytest.fixture()
def db(tmp_path):
    database = ChatHistoryDB(str(tmp_path / "chat_history.db"), default_source="cli")
    database.write_message("s1", "user", "我们在 gpu-worker 上部署了 autoresearch", source="cli")
    database.write_message("s1", "assistant", "好的，C++ 版本也编译过了", source="cli")
    yield database
    database.close()


def test_search_hyphen_token_returns_rows(db) -> None:
    rows = db.search("gpu-worker", top_k=5)
    assert rows, "含连字符的查询应该能命中"
    assert "gpu-worker" in rows[0]["snippet"]


def test_search_multi_word_uses_or(db) -> None:
    rows = db.search("autoresearch 完全不存在词", top_k=5)
    assert rows, "多词查询是 OR 语义，应该命中有 autoresearch 的那条"


def test_search_punctuation_only_does_not_raise(db) -> None:
    # 纯标点在 FTS5 里是空短语，允许返回空列表，但绝不能抛异常
    assert db.search("---", top_k=5) == [] or True
