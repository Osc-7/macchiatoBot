"""Remote worker client execution semantics tests."""

from __future__ import annotations

import asyncio

import pytest

from macchiato_remote.client import RemoteWorkerClient
from macchiato_remote.protocol import (
    RemoteCommandRequest,
    RemoteWorkspaceOpenRequest,
)

pytestmark = pytest.mark.asyncio


async def test_remote_execute_auto_background_on_wait_window(tmp_path):
    client = RemoteWorkerClient(server="http://127.0.0.1:9380", login="tester")
    session_id = "sid-auto-bg"
    try:
        opened = await client._open_workspace(
            RemoteWorkspaceOpenRequest(
                request_id="open-1",
                session_id=session_id,
                requested_path=str(tmp_path),
                profile="dev",
            )
        )
        assert opened.success
        result = await client._execute(
            RemoteCommandRequest(
                request_id="exec-1",
                session_id=session_id,
                command="sleep 0.5 && echo done",
                timeout_seconds=5,
                wait_window_ms=20,
            )
        )
        assert result.backgrounded is True
        assert result.job_id
        # give watcher a short chance to settle to avoid teardown races
        await asyncio.sleep(0.05)
    finally:
        await client.close()


async def test_remote_execute_wait_for_completion_disables_auto_background(tmp_path):
    client = RemoteWorkerClient(server="http://127.0.0.1:9380", login="tester")
    session_id = "sid-wait-complete"
    try:
        opened = await client._open_workspace(
            RemoteWorkspaceOpenRequest(
                request_id="open-2",
                session_id=session_id,
                requested_path=str(tmp_path),
                profile="dev",
            )
        )
        assert opened.success
        result = await client._execute(
            RemoteCommandRequest(
                request_id="exec-2",
                session_id=session_id,
                command="sleep 0.2 && echo done",
                timeout_seconds=5,
                wait_window_ms=20,
                wait_for_completion=True,
            )
        )
        assert result.backgrounded is False
        assert result.exit_code == 0
        assert "done" in result.stdout
        await asyncio.sleep(0.05)
    finally:
        await client.close()


async def test_close_workspace_restores_leaked_task_cancelling(tmp_path, monkeypatch):
    from macchiato_remote.protocol import (
        RemoteMcpShutdownRequest,
        RemoteWorkspaceCloseRequest,
    )

    client = RemoteWorkerClient(server="http://127.0.0.1:9380", login="tester")
    session_id = "sid-cancel-leak"
    try:
        opened = await client._open_workspace(
            RemoteWorkspaceOpenRequest(
                request_id="open-leak",
                session_id=session_id,
                requested_path=str(tmp_path),
                profile="dev",
            )
        )
        assert opened.success

        async def leaky_shutdown(sid, server_name=None):
            t = asyncio.current_task()
            assert t is not None
            # Simulate dual un-cleared cancel() before RuntimeError (old anyio bug)
            t.cancel()
            t.cancel()
            raise RuntimeError("Attempted to exit cancel scope in a different task")

        monkeypatch.setattr(client._mcp_host, "shutdown", leaky_shutdown)

        mcp_res = await client._mcp_shutdown(
            RemoteMcpShutdownRequest(
                request_id="mcp-shut-1",
                session_id=session_id,
                server_name="gpu_worker",
            )
        )
        assert mcp_res.closed == []
        await asyncio.sleep(0)

        close_res = await client._close_workspace(
            RemoteWorkspaceCloseRequest(
                request_id="close-leak",
                session_id=session_id,
            )
        )
        assert close_res.success is True
        # Must NOT raise CancelledError on the very next await!
        await asyncio.sleep(0)
        cur = asyncio.current_task()
        assert cur is not None and cur.cancelling() == 0
    finally:
        await client.close()
