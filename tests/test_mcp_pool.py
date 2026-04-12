"""外部 MCP 共享池单元测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_core.config import MCPServerConfig
from agent_core.mcp.pool import (
    SharedMCPPool,
    mcp_pool_identity_key,
    reset_shared_mcp_pool_for_tests,
    should_pool_stdio_server,
)


def test_should_pool_skips_schedule_tools() -> None:
    ext = MCPServerConfig(
        name="tavily",
        transport="stdio",
        command="npx",
        args=["-y", "tavily-mcp@latest"],
    )
    assert should_pool_stdio_server(ext) is True

    local = MCPServerConfig(
        name="schedule_tools",
        transport="stdio",
        command="python",
        args=["mcp_server.py"],
    )
    assert should_pool_stdio_server(local) is False

    local2 = MCPServerConfig(
        name="foo",
        transport="stdio",
        command="python3",
        args=["-m", "something", "mcp_server.py"],
    )
    assert should_pool_stdio_server(local2) is False


def test_pool_identity_key_ignores_name() -> None:
    a = MCPServerConfig(
        name="a",
        transport="stdio",
        command="npx",
        args=["-y", "pkg"],
    )
    b = MCPServerConfig(
        name="b",
        transport="stdio",
        command="npx",
        args=["-y", "pkg"],
    )
    assert mcp_pool_identity_key(a) == mcp_pool_identity_key(b)


@pytest.mark.asyncio
async def test_pool_acquire_release_refcount() -> None:
    server = MCPServerConfig(
        name="mock_ext",
        transport="stdio",
        command="npx",
        args=["-y", "fake"],
    )

    class _FakeSession:
        def __init__(self) -> None:
            self.list_tools = AsyncMock(
                return_value=SimpleNamespace(
                    tools=[
                        SimpleNamespace(
                            name="search",
                            description="d",
                            inputSchema={"type": "object", "properties": {}},
                        )
                    ]
                )
            )

    fake_stack = AsyncMock()
    fake_stack.aclose = AsyncMock()
    fake_runtime = SimpleNamespace(
        config=server,
        session=_FakeSession(),
    )

    pool = SharedMCPPool()
    key = mcp_pool_identity_key(server)

    with patch(
        "agent_core.mcp.pool.connect_stdio_mcp_with_retries",
        new=AsyncMock(return_value=(fake_stack, fake_runtime)),
    ):
        r1 = await pool.acquire(server)
        r2 = await pool.acquire(server)
        assert r1 is not None and r2 is not None
        assert r1[0] == key == r2[0]
        assert r1[1] is r2[1]
        assert len(r1[2]) == 1

    assert key in pool._entries
    assert pool._entries[key].refcount == 2

    await pool.release(key)
    assert pool._entries[key].refcount == 1

    await pool.release(key)
    assert key not in pool._entries
    fake_stack.aclose.assert_awaited_once()


def teardown_module() -> None:
    reset_shared_mcp_pool_for_tests()
