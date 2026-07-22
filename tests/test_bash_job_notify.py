from __future__ import annotations

import asyncio

import pytest

from agent_core.job_manager import get_job_manager
from agent_core.tools.bash_job_notify import (
    clear_all_tracking_for_tests,
    poll_completed_notifications,
    register_local_job,
    register_remote_job,
    stage_notification,
    suppress_job_notification,
)

pytestmark = pytest.mark.asyncio


async def test_local_background_notification_emits_once(tmp_path):
    clear_all_tracking_for_tests()
    session_id = "sid-local-notify"
    ws = str(tmp_path)
    mgr = get_job_manager(workspace_root=ws)
    handle = await mgr.start_job("sleep 0.4", cwd=ws, env={}, timeout_seconds=5)
    register_local_job(
        session_id=session_id,
        job_id=handle.job_id,
        command=handle.command,
        cwd=handle.cwd,
        log_path=str(handle.log_path),
        workspace_root=ws,
    )

    # 任务仍在运行时不应发通知。
    early = await poll_completed_notifications(session_id=session_id)
    assert early == []

    await asyncio.sleep(0.6)
    notes = await poll_completed_notifications(session_id=session_id)
    assert len(notes) == 1
    note = notes[0]
    assert note["job_id"] == handle.job_id
    assert note["status"] in {"finished", "failed", "timed_out", "cancelled"}
    assert note["remote"] is False

    # 终态通知只应出现一次。
    again = await poll_completed_notifications(session_id=session_id)
    assert again == []


class _FakeRemoteRegistry:
    def __init__(self) -> None:
        self.calls = 0

    async def job_status(self, **kwargs):
        self.calls += 1
        status = "running" if self.calls == 1 else "finished"
        return type(
            "RemoteStatus",
            (),
            {
                "status": status,
                "exit_code": 0,
                "timed_out": False,
                "duration_seconds": 1.2,
                "log_path": "/tmp/remote-job.log",
                "error": None,
            },
        )()


async def test_remote_background_notification_emits_on_terminal(monkeypatch):
    clear_all_tracking_for_tests()
    fake = _FakeRemoteRegistry()
    import agent_core.remote.worker_registry as wr

    monkeypatch.setattr(wr, "get_remote_worker_registry", lambda: fake)
    session_id = "sid-remote-notify"
    register_remote_job(
        session_id=session_id,
        remote_login="sii",
        job_id="job_remote_1",
        command="sleep 1 && echo done",
        cwd="/workspace",
        log_path="/tmp/remote-job.log",
    )

    first = await poll_completed_notifications(session_id=session_id)
    assert first == []

    second = await poll_completed_notifications(session_id=session_id)
    assert len(second) == 1
    assert second[0]["job_id"] == "job_remote_1"
    assert second[0]["status"] == "finished"
    assert second[0]["remote"] is True
    assert second[0]["remote_login"] == "sii"


class _FakeRemoteRegistryJobNotFound:
    async def job_status(self, **kwargs):
        return type(
            "RemoteStatus",
            (),
            {
                "status": "",
                "exit_code": None,
                "timed_out": False,
                "duration_seconds": 0.0,
                "log_path": "/tmp/lost-job.log",
                "error": "JOB_NOT_FOUND",
            },
        )()


async def test_remote_job_not_found_does_not_emit_false_failure(monkeypatch):
    """Worker 重启丢失 job 表时不应误报 failed 终态通知。"""
    clear_all_tracking_for_tests()
    fake = _FakeRemoteRegistryJobNotFound()
    import agent_core.remote.worker_registry as wr

    monkeypatch.setattr(wr, "get_remote_worker_registry", lambda: fake)
    session_id = "sid-remote-lost"
    job_id = "job_lost_1"
    register_remote_job(
        session_id=session_id,
        remote_login="sii",
        job_id=job_id,
        command="long-running-task",
        cwd="/workspace",
        log_path="/tmp/lost-job.log",
    )

    notes = await poll_completed_notifications(session_id=session_id)
    assert notes == []

    from agent_core.tools import bash_job_notify as bjn

    assert job_id in {
        rec.job_id for rec in bjn._TRACKED_BY_SESSION.get(session_id, {}).values()
    }


async def test_local_job_missing_from_manager_does_not_emit_false_failure(tmp_path):
    """daemon 重启后本地 JobManager 无记录时不应误报 failed。"""
    clear_all_tracking_for_tests()
    session_id = "sid-local-lost"
    job_id = "job_local_missing"
    register_local_job(
        session_id=session_id,
        job_id=job_id,
        command="sleep 99",
        cwd=str(tmp_path),
        log_path=str(tmp_path / "job.log"),
        workspace_root=str(tmp_path),
    )

    notes = await poll_completed_notifications(session_id=session_id)
    assert notes == []

    from agent_core.tools import bash_job_notify as bjn

    assert job_id in {
        rec.job_id for rec in bjn._TRACKED_BY_SESSION.get(session_id, {}).values()
    }


async def test_suppress_job_notification_skips_agent_stopped_job(monkeypatch):
    """Agent 主动 job_stop 后不应再发出终态通知（含已 staged 的 pending）。"""
    clear_all_tracking_for_tests()
    fake = _FakeRemoteRegistry()
    import agent_core.remote.worker_registry as wr

    monkeypatch.setattr(wr, "get_remote_worker_registry", lambda: fake)
    session_id = "sid-suppress-stop"
    job_id = "job_stop_me"
    register_remote_job(
        session_id=session_id,
        remote_login="sii",
        job_id=job_id,
        command="sleep 99",
        cwd="/workspace",
        log_path="/tmp/stop-me.log",
    )
    stage_notification(
        session_id,
        f"[后台任务完成] 远程任务 {job_id} 已结束：cancelled",
        note={
            "job_id": job_id,
            "status": "cancelled",
            "remote": True,
            "remote_login": "sii",
        },
    )

    assert suppress_job_notification(session_id, job_id, remote=True) is True

    notes = await poll_completed_notifications(session_id=session_id)
    assert notes == []

    from agent_core.tools import bash_job_notify as bjn

    assert bjn._PENDING_BY_SESSION.get(session_id, []) == []

