"""
水源社区 SQLite 数据库。

每用户独立 DB：data/shuiyuan/users/{username}/shuiyuan.db
- 聊天记录：每用户保留最近 N 条
- 限流：每用户每分钟最多 M 次
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Generator, List, Optional

# 用户名中非法文件名字符替换为 _
def _sanitize_username(username: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", (username or "").strip()) or "default"


def get_shuiyuan_db_path_for_user(base_dir: str, username: str) -> str:
    """每用户 DB 路径：{base}/users/{sanitized_username}/shuiyuan.db"""
    base = Path(base_dir or "./data/shuiyuan").resolve()
    safe = _sanitize_username(username)
    return str(base / "users" / safe / "shuiyuan.db")


_DDL = """
CREATE TABLE IF NOT EXISTS shuiyuan_chat (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT    NOT NULL,
    topic_id     INTEGER NOT NULL,
    post_id      INTEGER,
    role         TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shuiyuan_chat_username ON shuiyuan_chat(username);
CREATE INDEX IF NOT EXISTS idx_shuiyuan_chat_username_created ON shuiyuan_chat(username, created_at DESC);

CREATE TABLE IF NOT EXISTS shuiyuan_rate_limit (
    username     TEXT    NOT NULL,
    replied_at   TEXT    NOT NULL,
    PRIMARY KEY (username, replied_at)
);

CREATE INDEX IF NOT EXISTS idx_shuiyuan_rate_limit_username ON shuiyuan_rate_limit(username);
"""


class ShuiyuanDB:
    """
    水源社区数据库。

    - 聊天记录：按 username 分流，append 后自动 trim 到 chat_limit_per_user
    - 限流：滑动窗口，replies_per_minute 条/分钟
    """

    def __init__(
        self,
        db_path: str = "./data/shuiyuan/shuiyuan.db",
        chat_limit_per_user: int = 100,
        replies_per_minute: int = 5,
    ):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._chat_limit = chat_limit_per_user
        self._replies_per_minute = replies_per_minute
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
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
        conn.commit()

    def append_chat(
        self,
        username: str,
        topic_id: int,
        role: str,
        content: str,
        post_id: Optional[int] = None,
    ) -> None:
        """追加一条聊天记录，并 trim 该用户到 chat_limit 条。"""
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO shuiyuan_chat (username, topic_id, post_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (username, topic_id, post_id, role, content, now),
            )
            # 保留最近 chat_limit 条，超出直接删除
            cur.execute("SELECT COUNT(*) FROM shuiyuan_chat WHERE username = ?", (username,))
            cnt = cur.fetchone()[0]
            if cnt > self._chat_limit:
                to_evict = cnt - self._chat_limit
                cur.execute(
                    """
                    DELETE FROM shuiyuan_chat WHERE id IN (
                        SELECT id FROM shuiyuan_chat WHERE username = ?
                        ORDER BY created_at ASC LIMIT ?
                    )
                    """,
                    (username, to_evict),
                )

    def get_chat(self, username: str, limit: Optional[int] = None) -> List[dict]:
        """获取该用户最近的聊天记录，按时间升序（旧→新）。"""
        n = limit or self._chat_limit
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT id, username, topic_id, post_id, role, content, created_at
                FROM shuiyuan_chat WHERE username = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (username, n),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def check_reply_allowed(self, username: str) -> bool:
        """检查该用户是否允许回复（未超限）。"""
        now = datetime.now(timezone.utc)
        window_start = (now - timedelta(minutes=1)).isoformat()
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM shuiyuan_rate_limit
                WHERE username = ? AND replied_at >= ?
                """,
                (username, window_start),
            )
            count = cur.fetchone()[0]
        return count < self._replies_per_minute

    def record_reply(self, username: str) -> None:
        """记录一次回复，用于限流。"""
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO shuiyuan_rate_limit (username, replied_at) VALUES (?, ?)",
                (username, now),
            )
        # 清理 1 分钟前的记录
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        with self._cursor() as cur:
            cur.execute("DELETE FROM shuiyuan_rate_limit WHERE replied_at < ?", (cutoff,))
