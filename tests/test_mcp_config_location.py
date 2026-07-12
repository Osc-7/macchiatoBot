"""MCPServerConfig location/attach_on and overlay naming helpers."""

import pytest
from pydantic import ValidationError

from agent_core.config import MCPServerConfig
from agent_core.mcp.client import MCPClientManager, mcp_openai_safe_local_name
from agent_core.config import MCPConfig


def test_local_requires_command():
    with pytest.raises(ValidationError):
        MCPServerConfig(name="x", location="local", command=None)


def test_remote_allows_missing_command_and_defaults_attach():
    s = MCPServerConfig(name="chrome", location="remote")
    assert s.command is None
    assert s.attach_on == "remote_use"


def test_local_rejects_remote_use_attach():
    with pytest.raises(ValidationError):
        MCPServerConfig(
            name="tavily",
            location="local",
            command="npx",
            attach_on="remote_use",
        )


def test_manager_skips_remote_on_connect(monkeypatch):
    cfg = MCPConfig(
        enabled=True,
        servers=[
            MCPServerConfig(
                name="remote_only",
                location="remote",
                enabled=True,
            )
        ],
    )
    mgr = MCPClientManager(cfg)

    async def _boom(*_a, **_k):
        raise AssertionError("should not connect remote")

    monkeypatch.setattr(
        "agent_core.mcp.client.connect_stdio_mcp_with_retries", _boom
    )

    import asyncio

    async def _run():
        await mgr.connect()

    asyncio.run(_run())
    assert mgr.get_proxy_tools() == []


def test_name_collision_prefix():
    a = mcp_openai_safe_local_name("tavily", "search")
    b = mcp_openai_safe_local_name("r__tavily", "search")
    assert a != b
    assert "tavily" in a
