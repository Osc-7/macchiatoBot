"""本地 job 管理器对多行命令（heredoc）的回归。

bug 现场：`agent_core/job_manager.py` 把命令包成 `({command}) > log 2>&1`，
命令不以换行结尾时 heredoc 的终止符会和 `)` 粘成 `EOF)`，shell 报语法错、
退出码 2、错误进了日志而工具侧只看到空输出。
"""
from __future__ import annotations

import asyncio

import pytest

from agent_core.job_manager import JobManager


async def _run(command: str, tmp_path) -> tuple[int | None, str]:
    manager = JobManager(workspace_root=tmp_path)
    handle = await manager.start_job(command, cwd=str(tmp_path), timeout_seconds=30)
    for _ in range(80):
        await asyncio.sleep(0.1)
        status = await manager.job_status(handle.job_id)
        if status is not None and status.status != "running":
            break
    status = await manager.job_status(handle.job_id)
    log = status.log_path.read_text(errors="replace") if status and status.log_path.exists() else ""
    return getattr(status, "exit_code", None), log


@pytest.mark.asyncio
async def test_heredoc_python(tmp_path) -> None:
    exit_code, log = await _run("python3 - <<'EOF'\nprint('job-heredoc-ok')\nEOF", tmp_path)
    assert exit_code == 0, f"exit={exit_code} log={log!r}"
    assert "job-heredoc-ok" in log


@pytest.mark.asyncio
async def test_heredoc_cat(tmp_path) -> None:
    exit_code, log = await _run("cat <<'P'\nalpha\nbeta\nP", tmp_path)
    assert exit_code == 0, f"exit={exit_code} log={log!r}"
    assert "alpha" in log and "beta" in log


@pytest.mark.asyncio
async def test_multiline_for_loop(tmp_path) -> None:
    exit_code, log = await _run("for i in 1 2 3\ndo\n echo n=$i\ndone", tmp_path)
    assert exit_code == 0, f"exit={exit_code} log={log!r}"
    assert "n=3" in log


@pytest.mark.asyncio
async def test_single_line_still_works(tmp_path) -> None:
    exit_code, log = await _run("echo plain", tmp_path)
    assert exit_code == 0
    assert "plain" in log
