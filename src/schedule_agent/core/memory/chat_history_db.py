"""
ChatHistoryDB - 对话历史持久化数据库

使用 SQLite + FTS5 全文索引存储对话历史，支持关键词搜索和上下文翻页。
写入策略：
- user/assistant 消息：完整内容
- tool 消息：截断到 500 字并标注 [已截断]
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional


_TOOL_CONTENT_MAX = 500
_TRUNCATE_MARKER = "[已截断]"

_DDL = """
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    NOT NULL,
    source       TEXT    NOT NULL DEFAULT '',
    timestamp    TEXT    NOT NULL,
    role         TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    tool_name    TEXT,
    is_truncated INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    role,
    session_id,
    content='messages',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content, role, session_id)
    VALUES (new.id, new.content, new.role, new.session_id);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, role, session_id)
    VALUES ('delete', old.id, old.content, old.role, old.session_id);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, role, session_id)
    VALUES ('delete', old.id, old.content, old.role, old.session_id);
    INSERT INTO messages_fts(rowid, content, role, session_id)
    VALUES (new.id, new.content, new.role, new.session_id);
END;
"""


class ChatHistoryDB:
    """SQLite + FTS5 对话历史数据库。"""

    def __init__(self, db_path: str, default_source: Optional[str] = None) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._default_source = (default_source or "").strip()
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    @contextmanager
    def _cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        conn = self._get_conn()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    def _ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(_DDL)
        self._ensure_source_column(conn)
        conn.commit()

    def _ensure_source_column(self, conn: sqlite3.Connection) -> None:
        cur = conn.execute("PRAGMA table_info(messages)")
        cols = {str(r[1]) for r in cur.fetchall()}
        if "source" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_source_session ON messages(source, session_id)")

    @staticmethod
    def _truncate_tool_content(content: str) -> tuple[str, bool]:
        """截断工具内容，返回 (处理后内容, 是否截断)。"""
        if len(content) <= _TOOL_CONTENT_MAX:
            return content, False
        return content[:_TOOL_CONTENT_MAX] + _TRUNCATE_MARKER, True

    def write_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_name: Optional[str] = None,
        timestamp: Optional[str] = None,
        source: Optional[str] = None,
    ) -> int:
        """
        写入一条消息。

        Args:
            session_id: 会话 ID
            role: 消息角色（user | assistant | tool）
            content: 消息内容
            tool_name: 工具名（仅 role=tool 时有值）
            timestamp: ISO 8601 时间戳，默认取当前 UTC 时间

        Returns:
            新记录的 rowid
        """
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        msg_source = (source or self._default_source or "").strip()
        is_truncated = 0

        if role == "tool":
            content, truncated = self._truncate_tool_content(content)
            is_truncated = 1 if truncated else 0

        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages (session_id, source, timestamp, role, content, tool_name, is_truncated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, msg_source, ts, role, content, tool_name, is_truncated),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def search(
        self, query: str, top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """
        FTS5 关键词搜索，返回最相关的消息片段。

        Args:
            query: 搜索关键词
            top_k: 返回条数上限

        Returns:
            消息列表，每条包含 id / timestamp / role / session_id / snippet
        """
        if not query.strip():
            return []

        # FTS5 查询需要转义特殊字符
        safe_query = _escape_fts_query(query)

        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    m.id,
                    m.session_id,
                    m.source,
                    m.timestamp,
                    m.role,
                    m.tool_name,
                    snippet(messages_fts, 0, '[', ']', '...', 16) AS snippet
                FROM messages_fts
                JOIN messages m ON m.id = messages_fts.rowid
                WHERE messages_fts MATCH ?
                  AND (? = '' OR m.source = ?)
                ORDER BY rank
                LIMIT ?
                """,
                (safe_query, self._default_source, self._default_source, top_k),
            )
            rows = cur.fetchall()

        return [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "source": r["source"],
                "timestamp": r["timestamp"],
                "role": r["role"],
                "tool_name": r["tool_name"],
                "snippet": r["snippet"],
            }
            for r in rows
        ]

    def get_context(
        self, message_id: int, n: int = 5
    ) -> List[Dict[str, Any]]:
        """
        获取指定消息前后各 n 条（同一 session 内）。

        Args:
            message_id: 中心消息 ID
            n: 前后各取几条

        Returns:
            消息列表（含中心消息，按时间顺序）
        """
        with self._cursor() as cur:
            # 先取中心消息的 session_id
            cur.execute(
                "SELECT session_id FROM messages WHERE id = ? AND (? = '' OR source = ?)",
                (message_id, self._default_source, self._default_source),
            )
            row = cur.fetchone()
            if row is None:
                return []
            session_id = row["session_id"]

            cur.execute(
                """
                SELECT id, session_id, source, timestamp, role, content, tool_name, is_truncated
                FROM messages
                WHERE session_id = ?
                  AND (? = '' OR source = ?)
                  AND id BETWEEN ? AND ?
                ORDER BY id
                """,
                (
                    session_id,
                    self._default_source,
                    self._default_source,
                    message_id - n,
                    message_id + n,
                ),
            )
            rows = cur.fetchall()

        return [_row_to_dict(r) for r in rows]

    def scroll(
        self,
        message_id: int,
        direction: str = "up",
        n: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        从指定消息向上或向下翻页。

        Args:
            message_id: 锚点消息 ID
            direction: "up" 向更早翻 | "down" 向更新翻
            n: 返回条数

        Returns:
            消息列表（按时间顺序）
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT session_id FROM messages WHERE id = ? AND (? = '' OR source = ?)",
                (message_id, self._default_source, self._default_source),
            )
            row = cur.fetchone()
            if row is None:
                return []
            session_id = row["session_id"]

            if direction == "up":
                cur.execute(
                    """
                    SELECT id, session_id, source, timestamp, role, content, tool_name, is_truncated
                    FROM messages
                    WHERE session_id = ?
                      AND (? = '' OR source = ?)
                      AND id < ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (session_id, self._default_source, self._default_source, message_id, n),
                )
                rows = list(reversed(cur.fetchall()))
            else:
                cur.execute(
                    """
                    SELECT id, session_id, source, timestamp, role, content, tool_name, is_truncated
                    FROM messages
                    WHERE session_id = ?
                      AND (? = '' OR source = ?)
                      AND id > ?
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (session_id, self._default_source, self._default_source, message_id, n),
                )
                rows = cur.fetchall()

        return [_row_to_dict(r) for r in rows]

    def get_session_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """获取某 session 的所有消息（按时间顺序）。"""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT id, session_id, source, timestamp, role, content, tool_name, is_truncated
                FROM messages
                WHERE session_id = ?
                  AND (? = '' OR source = ?)
                ORDER BY id
                """,
                (session_id, self._default_source, self._default_source),
            )
            rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_session_messages_after(
        self,
        session_id: str,
        after_id: int,
        *,
        roles: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取某 session 在指定 id 之后的消息（按时间顺序）。"""
        if limit is not None:
            limit = max(1, int(limit))
        if roles:
            placeholders = ",".join("?" for _ in roles)
            sql = f"""
                SELECT id, session_id, source, timestamp, role, content, tool_name, is_truncated
                FROM messages
                WHERE session_id = ?
                  AND (? = '' OR source = ?)
                  AND id > ?
                  AND role IN ({placeholders})
                ORDER BY id
            """
            params: List[Any] = [session_id, self._default_source, self._default_source, int(after_id), *roles]
        else:
            sql = """
                SELECT id, session_id, source, timestamp, role, content, tool_name, is_truncated
                FROM messages
                WHERE session_id = ?
                  AND (? = '' OR source = ?)
                  AND id > ?
                ORDER BY id
            """
            params = [session_id, self._default_source, self._default_source, int(after_id)]
        if limit is not None:
            sql = f"{sql}\nLIMIT ?"
            params.append(limit)
        with self._cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]

    def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "source": row["source"],
        "timestamp": row["timestamp"],
        "role": row["role"],
        "content": row["content"],
        "tool_name": row["tool_name"],
        "is_truncated": bool(row["is_truncated"]),
    }


def _escape_fts_query(query: str) -> str:
    """将自然语言查询转换为 FTS5 查询语句。

    目标：
    - 对用户友好：支持用「多个词」作为关键词，不要求理解 FTS5 语法
    - 行为直观：多个词时表示「匹配其中任意一个」（OR），单词时按原样匹配
    - 安全：简单转义双引号，避免语法错误
    """
    # 拆分为非空词
    words = [w.strip() for w in query.split() if w.strip()]
    if not words:
        return '""'

    # 转义双引号，但不强制加整体引号，保留 FTS5 默认分词与匹配行为
    escaped = [w.replace('"', '""') for w in words]

    if len(escaped) == 1:
        # 单个词：直接用该词，等价于「包含这个词」
        return escaped[0]

    # 多个词：使用 OR 连接，表示「包含任意一个关键词」
    return " OR ".join(escaped)
