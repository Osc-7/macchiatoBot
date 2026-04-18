"""水源 connector：后台回复任务调度与退出排空。"""

from __future__ import annotations

import asyncio

import pytest

from frontend.shuiyuan_integration.connector import (
    _drain_pending_reply_tasks,
    _schedule_background_reply,
)


@pytest.mark.asyncio
async def test_schedule_background_reply_runs_async_and_drains() -> None:
    done = asyncio.Event()

    async def work() -> None:
        await asyncio.sleep(0.02)
        done.set()

    pending: set[asyncio.Task[object]] = set()
    _schedule_background_reply(work(), pending_tasks=pending)
    assert len(pending) == 1
    assert not done.is_set()

    await _drain_pending_reply_tasks(pending)
    assert done.is_set()
    assert len(pending) == 0


@pytest.mark.asyncio
async def test_drain_cancels_on_timeout() -> None:
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(3600.0)

    pending: set[asyncio.Task[object]] = set()
    _schedule_background_reply(slow(), pending_tasks=pending)
    await asyncio.wait_for(started.wait(), timeout=2.0)

    await _drain_pending_reply_tasks(pending, timeout=0.05)
    assert len(pending) == 0
