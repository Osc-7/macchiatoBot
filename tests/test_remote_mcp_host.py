"""Workspace MCP config + host unit tests (no real MCP SDK required for config)."""

from pathlib import Path

from macchiato_remote.runtime.macchiato_dir import ensure_macchiato_layout
from macchiato_remote.runtime.mcp_config import (
    MCP_YAML_REL,
    find_server,
    load_workspace_mcp_config,
)
from macchiato_remote.runtime.mcp_host import RemoteMcpHost
import pytest


def test_ensure_layout_writes_mcp_yaml_template(tmp_path: Path):
    ensure_macchiato_layout(tmp_path, device_label="test")
    yaml_path = tmp_path / MCP_YAML_REL
    assert yaml_path.is_file()
    text = yaml_path.read_text(encoding="utf-8")
    assert "mcp:" in text
    assert "servers:" in text


def test_load_workspace_mcp_config(tmp_path: Path):
    ensure_macchiato_layout(tmp_path)
    yaml_path = tmp_path / MCP_YAML_REL
    yaml_path.write_text(
        """
mcp:
  servers:
    - name: local_chrome
      enabled: true
      command: npx
      args: ["-y", "chrome-devtools-mcp@latest"]
""",
        encoding="utf-8",
    )
    cfg = load_workspace_mcp_config(tmp_path)
    srv = find_server(cfg, "local_chrome")
    assert srv is not None
    assert srv.command == "npx"
    assert find_server(cfg, "missing") is None


@pytest.mark.asyncio
async def test_mcp_host_ensure_missing_sdk_or_config(tmp_path: Path):
    host = RemoteMcpHost()
    host.bind_workspace("sess-1", tmp_path)
    # no mcp.yaml yet
    rows = await host.ensure("sess-1", ["chrome"])
    assert rows[0]["ok"] is False
    assert rows[0]["error"] in {
        "MCP_CONFIG_MISSING",
        "MCP_SDK_MISSING",
        "SESSION_NOT_OPEN",
    }

    ensure_macchiato_layout(tmp_path)
    (tmp_path / MCP_YAML_REL).write_text(
        "mcp:\n  servers:\n    - name: chrome\n      command: /bin/false\n",
        encoding="utf-8",
    )
    rows2 = await host.ensure("sess-1", ["chrome"])
    # Either SDK missing in env, or connect fails — must not crash
    assert rows2[0]["name"] == "chrome"
    assert "ok" in rows2[0]


@pytest.mark.asyncio
async def test_mcp_host_multi_session_out_of_order_and_cross_task_shutdown(
    tmp_path: Path, monkeypatch
):
    """Regression: multiple sessions opening dual anyio TaskGroups (stdio_client +
    ClientSession) on the same main task and closing out-of-LIFO-order or from a
    different task must not leak task cancellations or raise CancelledError."""
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import sys
    import anyio

    ensure_macchiato_layout(tmp_path)
    (tmp_path / MCP_YAML_REL).write_text(
        "mcp:\n  servers:\n    - name: gpu_worker\n      command: /bin/true\n",
        encoding="utf-8",
    )

    active_transports: list[str] = []

    @asynccontextmanager
    async def fake_stdio_client(params):
        async with anyio.create_task_group() as tg:
            marker = f"{params.command}:{len(active_transports)}"
            active_transports.append(marker)

            async def _bg():
                await anyio.sleep_forever()

            tg.start_soon(_bg)
            try:
                yield ("read_stream", "write_stream")
            finally:
                tg.cancel_scope.cancel()
                active_transports.remove(marker)

    class FakeClientSession:
        def __init__(self, read, write):
            self._tg = None

        async def __aenter__(self):
            self._tg = anyio.create_task_group()
            await self._tg.__aenter__()

            async def _reader():
                await anyio.sleep_forever()

            self._tg.start_soon(_reader)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self._tg.cancel_scope.cancel()
            return await self._tg.__aexit__(exc_type, exc, tb)

        async def initialize(self):
            await asyncio.sleep(0)

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="gpu_servers",
                        description="list servers",
                        inputSchema={"type": "object", "properties": {}},
                    )
                ]
            )

        async def call_tool(self, name, arguments=None):
            return SimpleNamespace(
                isError=False,
                content=[{"type": "text", "text": f"ok:{name}"}],
                structuredContent=None,
            )

    fake_mcp = SimpleNamespace(
        ClientSession=FakeClientSession,
        StdioServerParameters=lambda **kw: SimpleNamespace(**kw),
    )
    fake_mcp_client = SimpleNamespace(stdio=SimpleNamespace(stdio_client=fake_stdio_client))
    fake_mcp_stdio = SimpleNamespace(stdio_client=fake_stdio_client)
    monkeypatch.setitem(sys.modules, "mcp", fake_mcp)
    monkeypatch.setitem(sys.modules, "mcp.client", fake_mcp_client)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", fake_mcp_stdio)

    host = RemoteMcpHost()
    sessions = ["main", "sub:1", "sub:2", "sub:3"]
    for sid in sessions:
        host.bind_workspace(sid, tmp_path)
        res = await host.ensure(sid, ["gpu_worker"])
        assert res == [{"name": "gpu_worker", "ok": True, "error": None}]

    assert len(active_transports) == 4

    # Close middle session sub:2 out-of-LIFO-order on the same caller task:
    closed_mid = await host.shutdown("sub:2")
    assert closed_mid == ["gpu_worker"]
    # Simulate client.py next `await self._send_json(response)` — previously raised CancelledError!
    await asyncio.sleep(0)
    cur = asyncio.current_task()
    assert cur is not None and cur.cancelling() == 0
    assert len(active_transports) == 3

    # Close sub:1 from a completely different asyncio.Task:
    closed_cross = await asyncio.create_task(host.shutdown("sub:1"))
    assert closed_cross == ["gpu_worker"]
    await asyncio.sleep(0)
    assert len(active_transports) == 2

    # Remaining sessions main & sub:3 must still list & call tools normally:
    listed = await host.list_tools("sub:3", "gpu_worker")
    assert listed["error"] is None and listed["tools"][0]["name"] == "gpu_servers"
    called = await host.call_tool("main", "gpu_worker", "gpu_servers", {})
    assert called["is_error"] is False
    assert called["content"][0]["text"] == "ok:gpu_servers"

    # Finally shutdown_all cleans up everything cleanly:
    closed_all = await host.shutdown_all()
    assert len(closed_all) == 2
    assert active_transports == []
    assert cur.cancelling() == 0
