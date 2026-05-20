"""JobManager 单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from agent_core.job_manager import JobStatus, get_job_manager

pytestmark = pytest.mark.asyncio


async def test_background_job_finishes_and_tail_offset(tmp_path):
  manager = get_job_manager(workspace_root=str(tmp_path))
  handle = await manager.start_job(
    "for i in 1 2 3; do echo line$i; done",
    cwd=str(tmp_path),
    timeout_seconds=10,
  )
  assert handle.job_id.startswith("job_")

  for _ in range(50):
    if not handle.is_alive:
      break
    await asyncio.sleep(0.05)

  status = await manager.job_status(handle.job_id)
  assert status is not None
  assert status.status == JobStatus.FINISHED
  assert status.exit_code == 0

  first = await manager.job_tail(handle.job_id, lines=2, offset=0)
  assert first is not None
  assert first["total_lines"] == 3
  assert first["offset"] == 2
  assert "line1" in first["tail_lines"][0]

  second = await manager.job_tail(handle.job_id, lines=10, offset=first["offset"])
  assert second is not None
  assert second["offset"] == 3
  assert second["tail_lines"] == ["line3"]


async def test_stop_job(tmp_path):
  manager = get_job_manager(workspace_root=str(tmp_path))
  handle = await manager.start_job(
    "sleep 120",
    cwd=str(tmp_path),
    timeout_seconds=None,
  )
  assert handle.is_alive
  ok = await manager.stop_job(handle.job_id)
  assert ok
  await asyncio.sleep(0.1)
  status = await manager.job_status(handle.job_id)
  assert status is not None
  assert status.status == JobStatus.CANCELLED
