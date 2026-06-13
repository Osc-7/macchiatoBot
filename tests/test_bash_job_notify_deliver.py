from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from agent_core.kernel_interface.action import KernelRequest
from agent_core.tools.bash_job_notify import (
    clear_all_tracking_for_tests,
    deliver_via_inject,
    flush_pending_for_session,
    format_notification,
    poll_terminal_jobs,
    register_local_job,
    set_notify_dependencies,
    stage_notification,
)

pytestmark = pytest.mark.asyncio


class _FakeScheduler:
    def __init__(self) -> None:
        self.inflight: Dict[str, int] = {}
        self.injected: List[KernelRequest] = []

    def session_inflight_request_count(self, session_id: str) -> int:
        return self.inflight.get(session_id, 0)

    def inject_turn(self, request: KernelRequest) -> None:
        self.injected.append(request)


class _FakeCorePool:
    def __init__(self) -> None:
        self.entries: Dict[str, Any] = {}

    def get_live_entry(self, session_id: str):
        return self.entries.get(session_id)


def _note(
    session_id: str = "sid-1",
    job_id: str = "job-1",
    status: str = "finished",
    remote: bool = False,
    frontend_id: str = "cli",
    exit_code: int = 0,
    duration_seconds: float = 1.5,
    **kwargs: Any,
) -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "job_id": job_id,
        "status": status,
        "exit_code": exit_code,
        "duration_seconds": duration_seconds,
        "timed_out": False,
        "command": "echo done",
        "cwd": "/tmp",
        "log_path": "/tmp/job.log",
        "remote": remote,
        "remote_login": "a100" if remote else None,
        "frontend_id": frontend_id,
        "source": "cli",
        "user_id": "root",
        "metadata": {},
    }


async def test_format_notification():
    text = format_notification(_note(status="failed", exit_code=1))
    assert "[后台任务完成]" in text
    assert "job-1" in text
    assert "failed" in text
    assert "exit=1" in text
    assert "job_tail" in text
    assert "无需反复 job_status 轮询" in text

    remote_text = format_notification(_note(remote=True, remote_login="a100"))
    assert "远程(a100)" in remote_text


async def test_deliver_inject_turn_when_no_inflight(tmp_path):
    clear_all_tracking_for_tests()
    scheduler = _FakeScheduler()
    pool = _FakeCorePool()
    set_notify_dependencies(scheduler=scheduler, core_pool=pool)

    ws = str(tmp_path)
    register_local_job(
        session_id="sid-1",
        job_id="job-1",
        command="echo done",
        cwd=ws,
        log_path=f"{ws}/job.log",
        workspace_root=ws,
    )

    notes = await poll_terminal_jobs(max_items=10)
    assert len(notes) == 1

    ok = deliver_via_inject(
        session_id="sid-1",
        text=format_notification(notes[0]),
        note=notes[0],
    )
    assert ok is True
    assert len(scheduler.injected) == 1
    req = scheduler.injected[0]
    assert req.session_id == "sid-1"
    assert req.frontend_id == "bash_job"
    assert req.priority == -1
    assert "job-1" in req.text


async def test_deliver_stages_when_inflight(tmp_path):
    clear_all_tracking_for_tests()
    scheduler = _FakeScheduler()
    pool = _FakeCorePool()
    set_notify_dependencies(scheduler=scheduler, core_pool=pool)

    ws = str(tmp_path)
    register_local_job(
        session_id="sid-1",
        job_id="job-1",
        command="echo done",
        cwd=ws,
        log_path=f"{ws}/job.log",
        workspace_root=ws,
    )

    notes = await poll_terminal_jobs(max_items=10)
    scheduler.inflight["sid-1"] = 1

    ok = deliver_via_inject(
        session_id="sid-1",
        text=format_notification(notes[0]),
        note=notes[0],
    )
    assert ok is True
    assert len(scheduler.injected) == 0

    # flush 后注入并标记已通知
    scheduler.inflight["sid-1"] = 0
    flush_pending_for_session("sid-1")
    assert len(scheduler.injected) == 1
    assert scheduler.injected[0].session_id == "sid-1"
    # 同一 job 不会再次 poll 到
    notes_after = await poll_terminal_jobs(max_items=10)
    assert len(notes_after) == 0


async def test_staging_deduplicates_while_inflight(tmp_path):
    clear_all_tracking_for_tests()
    scheduler = _FakeScheduler()
    pool = _FakeCorePool()
    set_notify_dependencies(scheduler=scheduler, core_pool=pool)

    ws = str(tmp_path)
    register_local_job(
        session_id="sid-1",
        job_id="job-1",
        command="echo done",
        cwd=ws,
        log_path=f"{ws}/job.log",
        workspace_root=ws,
    )

    notes = await poll_terminal_jobs(max_items=10)
    assert len(notes) == 1
    note = notes[0]
    scheduler.inflight["sid-1"] = 1

    # 模拟 inflight 期间 watcher 多次 poll，同一 job 只应被暂存一次
    for _ in range(3):
        ok = deliver_via_inject(
            session_id="sid-1",
            text=format_notification(note),
            note=note,
        )
        assert ok is True
        # 已 staged，后续 poll 应返回空
        later = await poll_terminal_jobs(max_items=10)
        assert len(later) == 0

    assert len(scheduler.injected) == 0

    # flush 后只注入一次，并标记已通知
    scheduler.inflight["sid-1"] = 0
    flush_pending_for_session("sid-1")
    assert len(scheduler.injected) == 1


async def test_deliver_returns_false_without_scheduler():
    clear_all_tracking_for_tests()
    set_notify_dependencies(scheduler=None, core_pool=None)
    ok = deliver_via_inject(session_id="sid-1", text="hello")
    assert ok is False


async def test_deliver_via_inject_is_synchronous():
    """daemon watcher 直接同步调用 deliver_via_inject，不能是 coroutine。"""
    clear_all_tracking_for_tests()
    scheduler = _FakeScheduler()
    set_notify_dependencies(scheduler=scheduler, core_pool=None)

    ok = deliver_via_inject(session_id="sid-1", text="hello")
    assert isinstance(ok, bool)
    assert not asyncio.iscoroutine(ok)


async def test_stage_notification_fifo():
    clear_all_tracking_for_tests()
    stage_notification("sid-2", "first")
    stage_notification("sid-2", "second")

    scheduler = _FakeScheduler()
    set_notify_dependencies(scheduler=scheduler, core_pool=None)
    flush_pending_for_session("sid-2")
    assert [r.text for r in scheduler.injected] == ["first", "second"]


async def test_poll_terminal_jobs_limits_max_items(tmp_path):
    clear_all_tracking_for_tests()
    ws = str(tmp_path)
    for i in range(5):
        register_local_job(
            session_id="sid-multi",
            job_id=f"job-{i}",
            command="echo done",
            cwd=ws,
            log_path=f"{ws}/job-{i}.log",
            workspace_root=ws,
        )

    # 所有任务都已结束（echo 很快）
    await asyncio.sleep(0.2)
    notes = await poll_terminal_jobs(max_items=2)
    assert len(notes) == 2
