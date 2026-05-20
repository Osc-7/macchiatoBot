"""Remote worker job manager tests."""

from __future__ import annotations

import asyncio

import pytest

from macchiato_remote.runtime.jobs import RemoteSessionJobManager

pytestmark = pytest.mark.asyncio


async def test_remote_session_job_lifecycle(tmp_path):
    mgr = RemoteSessionJobManager(tmp_path)
    handle = await mgr.start_job(
        "for i in 1 2; do echo line$i; done",
        cwd=str(tmp_path),
        timeout_seconds=15,
    )
    for _ in range(40):
        if not handle.is_alive:
            break
        await asyncio.sleep(0.05)
    st = await mgr.job_status(handle.job_id)
    assert st is not None
    assert st.status == "finished"
    tail = await mgr.job_tail(handle.job_id, lines=10, offset=0)
    assert tail is not None
    assert "line1" in tail["tail_lines"][0]
