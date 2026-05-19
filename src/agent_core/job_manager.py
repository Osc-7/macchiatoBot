"""
JobManager -- 后台独立进程管理器。

为长命令（安装、下载、编译、训练等）提供独立的执行环境：
- 每个 job 拥有独立的 process group，timeout 时只杀 job 不碰主 shell
- stdout/stderr 重定向到持久化日志文件
- 支持 status 查询、tail 日志读取、手动终止

参考：
- Claude Code Bash tool run_in_background + BashOutput / KillShell
- MCP Tasks protocol (tasks/get, tasks/result, tasks/cancel)
- GNU coreutils timeout(1) --foreground / setpgid 行为
"""

from __future__ import annotations

import asyncio
import logging
import os
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
    env: Dict[str, str]
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


class JobManager:
    """管理后台独立进程。"""

    def __init__(self, workspace_root: Optional[str] = None) -> None:
        self._jobs: Dict[str, JobHandle] = {}
        self._workspace_root = Path(workspace_root or ".").resolve()
        self._lock = asyncio.Lock()

    # ── 公共 API ──────────────────────────────────────────────

    async def start_job(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> JobHandle:
        """
        启动一个后台独立进程。

        进程会被放入独立的 process group，timeout 时只杀该 group，
        不影响主 shell。
        """
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job_cwd = Path(cwd or self._workspace_root).resolve()
        if not job_cwd.is_dir():
            job_cwd = self._workspace_root

        log_dir = self._workspace_root / _JOB_LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job_id}.log"

        # 环境变量继承当前 os.environ，叠加用户传入的 env
        merged_env = {**os.environ, **(env or {})}

        handle = JobHandle(
            job_id=job_id,
            command=command,
            cwd=str(job_cwd),
            env=merged_env,
            log_path=log_path,
            timeout_seconds=timeout_seconds,
        )

        # 打开日志文件（覆盖写）
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=log_fd,
                stderr=log_fd,
                cwd=str(job_cwd),
                env=merged_env,
                start_new_session=True,  # 独立 process group
            )
        finally:
            os.close(log_fd)

        handle.process = proc
        async with self._lock:
            self._jobs[job_id] = handle

        # 启动 watcher 协程，监控进程结束和 timeout
        handle._watch_task = asyncio.create_task(
            self._watch_job(handle),
            name=f"job-watch-{job_id}",
        )

        logger.info(
            "Job started: %s pid=%s cmd=%s timeout=%s log=%s",
            job_id,
            proc.pid,
            command[:120],
            timeout_seconds,
            log_path,
        )
        return handle

    async def job_status(self, job_id: str) -> Optional[JobHandle]:
        """查询 job 当前状态。"""
        async with self._lock:
            return self._jobs.get(job_id)

    async def job_tail(
        self,
        job_id: str,
        *,
        lines: int = 200,
        offset: int = 0,
    ) -> Optional[Dict]:
        """
        读取 job 日志的尾部。

        返回包含 head_lines（前 N 行摘要）、tail_lines（尾部）、
        total_lines（总行数）、offset（下次读取起点）的字典。
        """
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

        # 读取全部行
        try:
            raw = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        all_lines = raw.splitlines()
        total = len(all_lines)

        # offset 允许从中间续读
        if offset < 0:
            offset = 0
        if offset > total:
            offset = total

        # head：前 50 行（只在 offset=0 时返回，帮助 Agent 看到开头）
        head = all_lines[:50] if offset == 0 else []

        # tail：从 offset 开始到末尾的最后 lines 行
        tail_start = max(offset, total - lines)
        tail = all_lines[tail_start:]

        return {
            "job_id": job_id,
            "status": handle.status,
            "head_lines": head,
            "tail_lines": tail,
            "total_lines": total,
            "offset": tail_start + len(tail),
            "log_path": str(log_path),
        }

    async def stop_job(
        self,
        job_id: str,
        *,
        signal_name: str = "SIGTERM",
    ) -> bool:
        """终止指定 job。"""
        handle = await self.job_status(job_id)
        if handle is None:
            return False
        if not handle.is_alive:
            return True

        sig = getattr(signal, signal_name.upper(), signal.SIGTERM)
        proc = handle.process
        if proc is None:
            return False

        # 杀整个 process group
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, OSError):
            try:
                proc.terminate()
            except ProcessLookupError:
                return False

        # 等待进程退出，超时后 SIGKILL
        try:
            await asyncio.wait_for(proc.wait(), timeout=_JOB_KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=_JOB_KILL_GRACE_SECONDS)
            except asyncio.TimeoutError:
                pass

        handle.status = JobStatus.CANCELLED
        handle.end_time = time.time()
        logger.info("Job stopped: %s", job_id)
        return True

    async def list_jobs(self) -> List[JobHandle]:
        """返回所有 job 的快照。"""
        async with self._lock:
            return list(self._jobs.values())

    async def cleanup_finished(self, max_age_seconds: float = 3600.0) -> int:
        """清理已完成且超过 max_age 的 job，释放内存。日志文件保留。"""
        now = time.time()
        removed = 0
        async with self._lock:
            finished = [
                jid
                for jid, h in self._jobs.items()
                if h.status != JobStatus.RUNNING
                and h.end_time is not None
                and (now - h.end_time) > max_age_seconds
            ]
            for jid in finished:
                del self._jobs[jid]
                removed += 1
        if removed:
            logger.info("JobManager cleaned up %d finished jobs", removed)
        return removed

    # ── 内部方法 ──────────────────────────────────────────────

    async def _watch_job(self, handle: JobHandle) -> None:
        """监控 job 进程直到结束或 timeout。"""
        proc = handle.process
        if proc is None:
            return

        timeout = handle.timeout_seconds
        try:
            if timeout is not None and timeout > 0:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            else:
                await proc.wait()
            # 正常结束
            handle.exit_code = proc.returncode
            handle.end_time = time.time()
            if proc.returncode == 0:
                handle.status = JobStatus.FINISHED
            else:
                handle.status = JobStatus.FAILED
        except asyncio.TimeoutError:
            # 超时：杀整个 process group
            handle.timed_out = True
            logger.warning(
                "Job timeout after %.1fs: %s", timeout or 0, handle.job_id
            )
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            # 给 grace period
            try:
                await asyncio.wait_for(proc.wait(), timeout=_JOB_KILL_GRACE_SECONDS)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(
                        proc.wait(), timeout=_JOB_KILL_GRACE_SECONDS
                    )
                except asyncio.TimeoutError:
                    pass
            handle.exit_code = proc.returncode if proc.returncode is not None else -1
            handle.status = JobStatus.TIMED_OUT
            handle.end_time = time.time()

        logger.info(
            "Job ended: %s status=%s exit_code=%s duration=%.1fs",
            handle.job_id,
            handle.status,
            handle.exit_code,
            handle.duration_seconds,
        )


# ── 全局单例（每个 workspace 一个）────────────────────────────

_job_managers: Dict[str, JobManager] = {}


def get_job_manager(workspace_root: Optional[str] = None) -> JobManager:
    """获取 workspace 对应的 JobManager 实例。"""
    key = str(Path(workspace_root or ".").resolve())
    if key not in _job_managers:
        _job_managers[key] = JobManager(workspace_root=key)
    return _job_managers[key]
