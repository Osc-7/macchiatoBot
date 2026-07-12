"""Thin helpers around remote MCP RPC (daemon side)."""

from __future__ import annotations

from typing import List, Optional, Sequence

from agent_core.config import Config, MCPServerConfig, get_config


def list_remote_mcp_server_configs(
    config: Optional[Config] = None,
) -> List[MCPServerConfig]:
    cfg = config or get_config()
    if not getattr(cfg, "mcp", None) or not cfg.mcp.enabled:
        return []
    out: List[MCPServerConfig] = []
    for server in cfg.mcp.servers:
        if not server.enabled:
            continue
        if getattr(server, "location", "local") != "remote":
            continue
        out.append(server)
    return out


def list_remote_mcp_server_names(config: Optional[Config] = None) -> List[str]:
    return [s.name for s in list_remote_mcp_server_configs(config)]


def remote_use_default_server_names(config: Optional[Config] = None) -> List[str]:
    return [
        s.name
        for s in list_remote_mcp_server_configs(config)
        if getattr(s, "attach_on", "remote_use") == "remote_use"
    ]


def worker_supports_remote_mcp(
    *,
    protocol_version: Optional[int],
    capabilities: Optional[Sequence[str]],
) -> bool:
    if protocol_version is not None and int(protocol_version) < 3:
        return False
    caps = set(capabilities or [])
    needed = {"mcp_ensure", "mcp_list_tools", "mcp_call_tool", "mcp_shutdown"}
    return needed.issubset(caps)
