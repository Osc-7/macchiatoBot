"""SQLite-backed persistent queue for AgentTask."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .agent_task import AgentTask, TaskStatus


def _default_db_path() -> Path:
    test_dir = os.environ.get("SCHEDULE_AGENT_TEST_DATA_DIR")
    if test_dir:
        return Path(test_dir) / "automation" / "agent_tasks.db"
    return Path("data") / "automation" / "agent_tasks.db"


class AgentTaskQueue:
    """
    SQLite 持久化 AgentTask 队列。

    进程重启后，处于 running 状态的任务可通过 recover_stale_running() 恢复为
    pending，确保任务不丢失。

    支持多进程并发写入（WAL 模式），单消费者轮询。
    """

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = Path(db_path) if db_path else _default_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id    TEXT PRIMARY KEY,
                    status     TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    data       TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_status_created ON agent_tasks (status, created_at)"
            )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def push(self, task: AgentTask) -> None:
        """将任务加入队列（状态重置为 pending）。"""
        task.status = TaskStatus.PENDING
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_tasks (task_id, status, created_at, data) VALUES (?, ?, ?, ?)",
                (
                    task.task_id,
                    task.status.value,
                    task.created_at.isoformat(),
                    task.model_dump_json(),
                ),
            )

    def pop_pending(self) -> Optional[AgentTask]:
        """
        原子地取出最早一条 pending 任务并将其标记为 running。

        若队列为空则返回 None。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_id, data FROM agent_tasks WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None

            task = AgentTask.model_validate_json(row["data"])
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.utcnow()
            conn.execute(
                "UPDATE agent_tasks SET status = 'running', data = ? WHERE task_id = ?",
                (task.model_dump_json(), task.task_id),
            )
            return task

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """更新任务状态、结果或错误信息。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM agent_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return
            task = AgentTask.model_validate_json(row["data"])
            task.status = status
            task.finished_at = datetime.utcnow()
            if result is not None:
                task.result = result
            if error is not None:
                task.error = error
            conn.execute(
                "UPDATE agent_tasks SET status = ?, data = ? WHERE task_id = ?",
                (status.value, task.model_dump_json(), task_id),
            )

    def list_recent(
        self,
        limit: int = 20,
        status: Optional[TaskStatus] = None,
    ) -> List[AgentTask]:
        """列出最近任务，可按状态过滤。"""
        with self._connect() as conn:
            if status is not None:
                rows = conn.execute(
                    "SELECT data FROM agent_tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status.value, max(1, limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT data FROM agent_tasks ORDER BY created_at DESC LIMIT ?",
                    (max(1, limit),),
                ).fetchall()
        return [AgentTask.model_validate_json(row["data"]) for row in rows]

    def recover_stale_running(self) -> int:
        """
        将所有处于 running 状态的任务重置为 pending。

        通常在进程启动时调用，用于恢复上次崩溃未完成的任务。
        返回恢复的任务数量。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, data FROM agent_tasks WHERE status = 'running'"
            ).fetchall()
            count = 0
            for row in rows:
                task = AgentTask.model_validate_json(row["data"])
                task.status = TaskStatus.PENDING
                task.started_at = None
                conn.execute(
                    "UPDATE agent_tasks SET status = 'pending', data = ? WHERE task_id = ?",
                    (task.model_dump_json(), task.task_id),
                )
                count += 1
        return count

    def pending_count(self) -> int:
        """返回当前 pending 任务数量。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM agent_tasks WHERE status = 'pending'"
            ).fetchone()
        return row[0] if row else 0
