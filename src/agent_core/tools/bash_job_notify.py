"""
后台 bash 任务完成通知注册表。

目标：
- 在 bash 启动后台任务时登记（本地 / 远程）；
- 在后续轮次由 AgentCore 轮询终态并注入一条轻量通知；
- 终态仅通知一次，避免重复噪声。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional

_TERMINAL_STATUSES = frozenset({"finished", "failed", "timed_out", "cancelled"})


@dataclass
class _TrackedJob:
    session_id: str
    job_id: str
    command: str
    cwd: str
    log_path: str
    created_at: float
    remote: bool
    workspace_root: Optional[str] = None
    remote_login: Optional[str] = None


_LOCK = Lock()
_TRACKED_BY_SESSION: Dict[str, Dict[str, _TrackedJob]] = {}


def _job_key(job_id: str, *, remote: bool) -> str:
    return f"{'r' if remote else 'l'}:{job_id}"


def register_local_job(
    *,
    session_id: str,
    job_id: str,
    command: str,
    cwd: str,
    log_path: str,
    workspace_root: str,
) -> None:
    sid = str(session_id or "").strip()
    jid = str(job_id or "").strip()
    if not sid or not jid:
        return
    rec = _TrackedJob(
        session_id=sid,
        job_id=jid,
        command=str(command or ""),
        cwd=str(cwd or ""),
        log_path=str(log_path or ""),
        workspace_root=str(workspace_root or ""),
        remote=False,
        created_at=time.time(),
    )
    with _LOCK:
        jobs = _TRACKED_BY_SESSION.setdefault(sid, {})
        jobs[_job_key(jid, remote=False)] = rec


def register_remote_job(
    *,
    session_id: str,
    remote_login: str,
    job_id: str,
    command: str,
    cwd: str,
    log_path: str,
) -> None:
    sid = str(session_id or "").strip()
    jid = str(job_id or "").strip()
    login = str(remote_login or "").strip()
    if not sid or not jid or not login:
        return
    rec = _TrackedJob(
        session_id=sid,
        job_id=jid,
        command=str(command or ""),
        cwd=str(cwd or ""),
        log_path=str(log_path or ""),
        remote=True,
        remote_login=login,
        created_at=time.time(),
    )
    with _LOCK:
        jobs = _TRACKED_BY_SESSION.setdefault(sid, {})
        jobs[_job_key(jid, remote=True)] = rec


async def poll_completed_notifications(
    *,
    session_id: str,
    max_items: int = 3,
) -> List[Dict[str, Any]]:
    """轮询指定 session 的后台任务终态通知（每个 job 只返回一次）。"""
    sid = str(session_id or "").strip()
    if not sid or max_items <= 0:
        return []
    with _LOCK:
        tracked = list(_TRACKED_BY_SESSION.get(sid, {}).items())
    if not tracked:
        return []

    out: List[Dict[str, Any]] = []
    to_remove: List[str] = []

    for key, rec in tracked:
        if len(out) >= max_items:
            break
        status_payload = await _query_job_status(rec)
        if status_payload is None:
            continue
        status = str(status_payload.get("status") or "").strip().lower()
        if status not in _TERMINAL_STATUSES:
            continue
        out.append(
            {
                "job_id": rec.job_id,
                "status": status,
                "exit_code": status_payload.get("exit_code"),
                "duration_seconds": status_payload.get("duration_seconds"),
                "timed_out": bool(status_payload.get("timed_out", False)),
                "command": rec.command,
                "cwd": rec.cwd,
                "log_path": rec.log_path or str(status_payload.get("log_path") or ""),
                "remote": rec.remote,
                "remote_login": rec.remote_login,
            }
        )
        to_remove.append(key)

    if to_remove:
        with _LOCK:
            jobs = _TRACKED_BY_SESSION.get(sid, {})
            for k in to_remove:
                jobs.pop(k, None)
            if not jobs:
                _TRACKED_BY_SESSION.pop(sid, None)
    return out


async def _query_job_status(rec: _TrackedJob) -> Optional[Dict[str, Any]]:
    try:
        if rec.remote:
            from agent_core.remote.worker_registry import get_remote_worker_registry

            registry = get_remote_worker_registry()
            res = await registry.job_status(
                login=str(rec.remote_login or ""),
                session_id=rec.session_id,
                job_id=rec.job_id,
            )
            if str(getattr(res, "error", "") or "") == "JOB_NOT_FOUND":
                return {
                    "status": "failed",
                    "exit_code": None,
                    "timed_out": False,
                    "duration_seconds": 0.0,
                    "log_path": rec.log_path,
                }
            return {
                "status": str(getattr(res, "status", "") or ""),
                "exit_code": getattr(res, "exit_code", None),
                "timed_out": bool(getattr(res, "timed_out", False)),
                "duration_seconds": float(getattr(res, "duration_seconds", 0.0) or 0.0),
                "log_path": str(getattr(res, "log_path", "") or rec.log_path),
            }

        from agent_core.job_manager import get_job_manager

        mgr = get_job_manager(workspace_root=rec.workspace_root or ".")
        handle = await mgr.job_status(rec.job_id)
        if handle is None:
            return {
                "status": "failed",
                "exit_code": None,
                "timed_out": False,
                "duration_seconds": 0.0,
                "log_path": rec.log_path,
            }
        return {
            "status": str(getattr(handle, "status", "") or ""),
            "exit_code": getattr(handle, "exit_code", None),
            "timed_out": bool(getattr(handle, "timed_out", False)),
            "duration_seconds": float(getattr(handle, "duration_seconds", 0.0) or 0.0),
            "log_path": str(getattr(handle, "log_path", rec.log_path)),
        }
    except Exception:
        # 轮询异常时保持静默，留待后续轮次继续尝试。
        return None


def clear_session_tracking_for_tests(session_id: str) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _LOCK:
        _TRACKED_BY_SESSION.pop(sid, None)


def clear_all_tracking_for_tests() -> None:
    with _LOCK:
        _TRACKED_BY_SESSION.clear()
