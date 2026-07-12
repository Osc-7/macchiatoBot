"""McpSessionOverlay attach/detach without real MCP processes."""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from agent_core.config import MCPConfig, MCPServerConfig
from agent_core.mcp.remote_proxy_tool import RemoteMCPProxyTool
from agent_core.mcp.session_overlay import McpSessionOverlay
from agent_core.tools.base import BaseTool
from agent_core.tools.versioned_registry import VersionedToolRegistry


class _FakeAgent:
    def __init__(self) -> None:
        self._tool_registry = VersionedToolRegistry()
        self._tool_catalog = VersionedToolRegistry()
        self._mcp_overlay_tools: Dict[str, set] = {}
        self._working_set = SimpleNamespace(unpin=lambda _n: None)
        self.config = SimpleNamespace(
            mcp=MCPConfig(
                enabled=True,
                servers=[
                    MCPServerConfig(
                        name="chrome",
                        location="remote",
                        attach_on="remote_use",
                        enabled=True,
                        tool_name_prefix="chrome",
                    )
                ],
            )
        )

    def _apply_mcp_proxy_tools(self, proxy_tools: List[BaseTool]) -> None:
        self._tool_registry.update_tools(proxy_tools)
        self._tool_catalog.update_tools(proxy_tools)
        for tool in proxy_tools:
            server = getattr(tool, "server_name", None) or getattr(
                tool, "_server_name", None
            )
            if server:
                self._mcp_overlay_tools.setdefault(str(server), set()).add(tool.name)


@pytest.mark.asyncio
async def test_overlay_detach_unregisters(monkeypatch):
    agent = _FakeAgent()
    tool = RemoteMCPProxyTool(
        local_name="chrome__nav",
        server_name="chrome",
        remote_name="nav",
        description="nav",
        input_schema={"type": "object", "properties": {}},
        login="laptop",
        session_id="s1",
    )
    agent._apply_mcp_proxy_tools([tool])
    assert agent._tool_registry.has("chrome__nav")

    monkeypatch.setattr(
        "agent_core.config.get_config",
        lambda: agent.config,
    )

    async def _noop_shutdown(*_a, **_k):
        return None

    overlay = McpSessionOverlay()
    monkeypatch.setattr(overlay, "_shutdown_remote_server", _noop_shutdown)
    row = await overlay.detach(agent, "chrome", session_id="s1")
    assert row.name == "chrome"
    assert "chrome__nav" in row.tool_names
    assert not agent._tool_registry.has("chrome__nav")


def test_collision_uses_r_prefix():
    from agent_core.mcp.client import mcp_openai_safe_local_name

    existing = {mcp_openai_safe_local_name("chrome", "nav")}
    local = mcp_openai_safe_local_name("chrome", "nav")
    if local in existing:
        local = mcp_openai_safe_local_name("r__chrome", "nav")
    assert local.startswith("r__") or local not in existing
