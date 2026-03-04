"""Persistent session registry for cross-process visibility."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List


_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    owner_id   TEXT NOT NULL,
    source     TEXT NOT NULL,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner_id, source, session_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_owner_source
ON sessions(owner_id, source, updated_at);
"""


class SessionRegistry:
    """SQLite-backed registry for session discovery across terminals."""

    def __init__(self, db_path: str = "./data/sessions/session_registry.db") -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_DDL)
        self._conn.commit()

    def upsert_session(self, owner_id: str, source: str, session_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO sessions(owner_id, source, session_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, source, session_id) DO UPDATE SET
                updated_at=excluded.updated_at
            """,
            (owner_id, source, session_id, now, now),
        )
        self._conn.commit()

    def session_exists(self, owner_id: str, source: str, session_id: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM sessions WHERE owner_id=? AND source=? AND session_id=? LIMIT 1",
            (owner_id, source, session_id),
        )
        return cur.fetchone() is not None

    def list_sessions(self, owner_id: str, source: str) -> List[str]:
        cur = self._conn.execute(
            """
            SELECT session_id
            FROM sessions
            WHERE owner_id=? AND source=?
            ORDER BY updated_at DESC
            """,
            (owner_id, source),
        )
        return [str(row[0]) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
