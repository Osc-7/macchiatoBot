"""Background jobs for a remote workspace session (worker-side)."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_JOB_LOG_DIR = ".macchiato/jobs"
_JOB_KILL_GRACE_SECONDS = 3


class JobStatus:
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass
class JobHandle:
    job_id: str
    command: str
    cwd: str
    log_path: Path
    timeout_seconds: Optional[float]
    process: Optional[asyncio.subprocess.Process] = None
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    status: str = JobStatus.RUNNING
    exit_code: Optional[int] = None
    timed_out: bool = False
    _watch_task: Optional[asyncio.Task] = None

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid if self.process else None

    @property
    def duration_seconds(self) -> float:
        end = self.end_time if self.end_time is not None else time.time()
        return end - self.start_time

    @property
    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None


class RemoteSessionJobManager:
    """Jobs scoped to one authorized workspace root on the worker."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.resolve()
        self._jobs: Dict[str, JobHandle] = {}
        self._lock = asyncio.Lock()

    async def start_job(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> JobHandle:
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job_cwd = Path(cwd or self._workspace_root).resolve()
        if not job_cwd.is_dir():
            job_cwd = self._workspace_root

        log_dir = self._workspace_root / _JOB_LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job_id}.log"

        merged_env = {**os.environ, **(env or {})}
        merged_env["HOME"] = str(self._workspace_root)
        merged_env["MACCHIATO_WORKSPACE_ROOT"] = str(self._workspace_root)

        handle = JobHandle(
            job_id=job_id,
            command=command,
            cwd=str(job_cwd),
            log_path=log_path,
            timeout_seconds=timeout_seconds,
        )

        quoted_log = shlex.quote(str(log_path))
        redirected_cmd = f"({command}) > {quoted_log} 2>&1"
        proc = await asyncio.create_subprocess_shell(
            redirected_cmd,
            cwd=str(job_cwd),
            env=merged_env,
            start_new_session=True,
        )
        handle.process = proc
        async with self._lock:
            self._jobs[job_id] = handle
        handle._watch_task = asyncio.create_task(
            self._watch_job(handle),
            name=f"remote-job-watch-{job_id}",
        )
        logger.info(
            "Remote job started: %s pid=%s cwd=%s",
            job_id,
            proc.pid,
            job_cwd,
        )
        return handle

    async def job_status(self, job_id: str) -> Optional[JobHandle]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def job_tail(
        self,
        job_id: str,
        *,
        lines: int = 200,
        offset: int = 0,
    ) -> Optional[Dict]:
        handle = await self.job_status(job_id)
        if handle is None:
            return None
        log_path = handle.log_path
        if not log_path.exists():
            return {
                "job_id": job_id,
                "status": handle.status,
                "head_lines": [],
                "tail_lines": [],
                "total_lines": 0,
                "offset": 0,
                "log_path": str(log_path),
            }
        try:
            raw = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        all_lines = raw.splitlines()
        total = len(all_lines)
        if offset < 0:
            offset = 0
        if offset >= total:
            return {
                "job_id": job_id,
                "status": handle.status,
                "head_lines": [],
                "tail_lines": [],
                "total_lines": total,
                "offset": total,
                "log_path": str(log_path),
            }
        tail_start = offset
        tail_end = min(tail_start + lines, total)
        tail = all_lines[tail_start:tail_end]
        head = all_lines[:50] if offset == 0 else []
        return {
            "job_id": job_id,
            "status": handle.status,
            "head_lines": head,
            "tail_lines": tail,
            "total_lines": total,
            "offset": tail_start + len(tail),
            "log_path": str(log_path),
        }

    async def stop_job(self, job_id: str, *, signal_name: str = "SIGTERM") -> bool:
        handle = await self.job_status(job_id)
        if handle is None or not handle.is_alive:
            return handle is not None
        sig = getattr(signal, signal_name.upper(), signal.SIGTERM)
        proc = handle.process
        if proc is None:
            return False
        await _terminate_process_tree(proc, sig)
        handle.status = JobStatus.CANCELLED
        handle.end_time = time.time()
        return True

    async def _watch_job(self, handle: JobHandle) -> None:
        proc = handle.process
        if proc is None:
            return
        timeout = handle.timeout_seconds
        try:
            if timeout is not None and timeout > 0:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            else:
                await proc.wait()
            if handle.status == JobStatus.CANCELLED:
                return
            handle.exit_code = proc.returncode
            handle.end_time = time.time()
            handle.status = (
                JobStatus.FINISHED if proc.returncode == 0 else JobStatus.FAILED
            )
        except asyncio.TimeoutError:
            handle.timed_out = True
            await _terminate_process_tree(proc, signal.SIGTERM)
            if handle.status != JobStatus.CANCELLED:
                handle.exit_code = proc.returncode if proc.returncode is not None else -1
                handle.status = JobStatus.TIMED_OUT
                handle.end_time = time.time()


class RemoteJobRegistry:
    """session_id -> job manager for open remote workspaces."""

    def __init__(self) -> None:
        self._managers: Dict[str, RemoteSessionJobManager] = {}

    def open_session(self, session_id: str, workspace_root: Path) -> None:
        self._managers[session_id] = RemoteSessionJobManager(workspace_root)

    def close_session(self, session_id: str) -> None:
        self._managers.pop(session_id, None)

    def get(self, session_id: str) -> Optional[RemoteSessionJobManager]:
        return self._managers.get(session_id)


async def _terminate_process_tree(
    proc: asyncio.subprocess.Process,
    sig: signal.Signals,
) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, OSError):
        try:
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_JOB_KILL_GRACE_SECONDS)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            try:
                proc.kill()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(proc.wait(), timeout=_JOB_KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            return
