"""Session-scoped MCP attach/detach overlay (local + remote)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Set

from agent_core.config import Config, MCPServerConfig, get_config
from agent_core.mcp.client import mcp_openai_safe_local_name
from agent_core.mcp.proxy_tool import MCPProxyTool
from agent_core.mcp.remote_proxy_tool import RemoteMCPProxyTool
from agent_core.remote.mcp_bridge import (
    list_remote_mcp_server_configs,
    remote_use_default_server_names,
    worker_supports_remote_mcp,
)
from agent_core.tools.base import BaseTool

logger = logging.getLogger(__name__)


@dataclass
class McpServerListRow:
    name: str
    location: Literal["local", "remote"]
    attach_on: str
    attached: bool
    tool_names: List[str] = field(default_factory=list)
    error: Optional[str] = None


class McpSessionOverlay:
    """Attach/detach declared MCP servers onto a session AgentCore."""

    def list_declared(self, agent: Any, config: Optional[Config] = None) -> List[McpServerListRow]:
        cfg = config or get_config()
        overlay = self._overlay_map(agent)
        rows: List[McpServerListRow] = []
        if not getattr(cfg, "mcp", None) or not cfg.mcp.enabled:
            return rows
        for server in cfg.mcp.servers:
            if not server.enabled:
                continue
            names = sorted(overlay.get(server.name, set()))
            rows.append(
                McpServerListRow(
                    name=server.name,
                    location=getattr(server, "location", "local"),  # type: ignore[arg-type]
                    attach_on=getattr(server, "attach_on", "boot"),
                    attached=bool(names),
                    tool_names=names,
                )
            )
        return rows

    async def attach(
        self, agent: Any, server_name: str, *, session_id: str
    ) -> McpServerListRow:
        cfg = get_config()
        server = self._find_server(cfg, server_name)
        if server is None:
            return McpServerListRow(
                name=server_name,
                location="local",
                attach_on="manual",
                attached=False,
                error="UNKNOWN_SERVER",
            )
        if not server.enabled:
            return McpServerListRow(
                name=server.name,
                location=getattr(server, "location", "local"),  # type: ignore[arg-type]
                attach_on=getattr(server, "attach_on", "boot"),
                attached=False,
                error="SERVER_DISABLED",
            )
        loc = getattr(server, "location", "local")
        if loc == "remote":
            return await self._attach_remote(agent, server, session_id=session_id)
        return await self._attach_local(agent, server)

    async def detach(
        self, agent: Any, server_name: str, *, session_id: str
    ) -> McpServerListRow:
        cfg = get_config()
        server = self._find_server(cfg, server_name)
        loc: Literal["local", "remote"] = "local"
        attach_on = "manual"
        if server is not None:
            loc = getattr(server, "location", "local")  # type: ignore[assignment]
            attach_on = getattr(server, "attach_on", "boot")

        overlay = self._overlay_map(agent)
        tool_names = list(overlay.get(server_name, set()))
        self._unregister_tools(agent, tool_names)
        overlay.pop(server_name, None)

        if loc == "remote":
            await self._shutdown_remote_server(agent, session_id, server_name)
        else:
            manager = getattr(agent, "_mcp_manager", None)
            if manager is not None:
                try:
                    await manager.disconnect_server(server_name)
                except Exception:
                    logger.exception("local mcp disconnect failed server=%s", server_name)

        return McpServerListRow(
            name=server_name,
            location=loc,
            attach_on=attach_on,
            attached=False,
            tool_names=tool_names,
        )

    async def reload(
        self, agent: Any, server_name: str, *, session_id: str
    ) -> McpServerListRow:
        await self.detach(agent, server_name, session_id=session_id)
        return await self.attach(agent, server_name, session_id=session_id)

    async def attach_defaults_for_remote_use(
        self, agent: Any, *, session_id: str
    ) -> List[McpServerListRow]:
        names = remote_use_default_server_names()
        rows: List[McpServerListRow] = []
        for name in names:
            rows.append(await self.attach(agent, name, session_id=session_id))
        return rows

    async def detach_all_remote(self, agent: Any, *, session_id: str) -> List[str]:
        detached: List[str] = []
        for server in list_remote_mcp_server_configs():
            row = await self.detach(agent, server.name, session_id=session_id)
            detached.append(row.name)
        # Also drop any overlay remote tools even if config changed
        overlay = self._overlay_map(agent)
        for name in list(overlay.keys()):
            tools = list(overlay.get(name, set()))
            if not tools:
                continue
            # Heuristic: RemoteMCPProxyTool instances
            reg = getattr(agent, "_tool_registry", None)
            sample = None
            if reg is not None and tools:
                sample = reg.get(tools[0]) if hasattr(reg, "get") else None
            if isinstance(sample, RemoteMCPProxyTool) or name in {
                s.name for s in list_remote_mcp_server_configs()
            }:
                await self.detach(agent, name, session_id=session_id)
                if name not in detached:
                    detached.append(name)
        try:
            from agent_core.remote.workspace_state import get_remote_workspace_state
            from agent_core.remote.worker_registry import get_remote_worker_registry

            state = get_remote_workspace_state(session_id)
            if state is not None:
                await get_remote_worker_registry().mcp_shutdown(
                    login=state.login, session_id=session_id, server_name=None
                )
        except Exception:
            logger.debug("mcp_shutdown all skipped", exc_info=True)
        return detached

    async def _attach_local(self, agent: Any, server: MCPServerConfig) -> McpServerListRow:
        manager = getattr(agent, "_mcp_manager", None)
        if manager is None:
            from agent_core.mcp import MCPClientManager

            try:
                runtime_mcp_cfg = agent.config.mcp.model_copy(deep=True)
            except Exception:
                runtime_mcp_cfg = agent.config.mcp
            manager = MCPClientManager(runtime_mcp_cfg)
            agent._mcp_manager = manager
            agent._mcp_connected = True

        try:
            proxies = await manager.connect_server(server.name)
        except Exception as exc:
            return McpServerListRow(
                name=server.name,
                location="local",
                attach_on=getattr(server, "attach_on", "boot"),
                attached=False,
                error=str(exc),
            )
        apply = getattr(agent, "_apply_mcp_proxy_tools", None)
        if apply:
            apply(list(proxies))
        else:
            self._register_tools(agent, list(proxies))
        names = [p.name for p in proxies]
        self._overlay_map(agent)[server.name] = set(names)
        # Also record boot-time tools already on manager for this server
        existing = manager.get_proxy_tools_for(server.name)
        if existing:
            names = [p.name for p in existing]
            self._overlay_map(agent)[server.name] = set(names)
            if not proxies:
                apply and apply(list(existing))
        return McpServerListRow(
            name=server.name,
            location="local",
            attach_on=getattr(server, "attach_on", "boot"),
            attached=True,
            tool_names=sorted(self._overlay_map(agent).get(server.name, set())),
        )

    async def _attach_remote(
        self, agent: Any, server: MCPServerConfig, *, session_id: str
    ) -> McpServerListRow:
        from agent_core.remote.workspace_state import get_remote_workspace_state
        from agent_core.remote.worker_registry import get_remote_worker_registry

        state = get_remote_workspace_state(session_id)
        if state is None:
            return McpServerListRow(
                name=server.name,
                location="remote",
                attach_on=getattr(server, "attach_on", "remote_use"),
                attached=False,
                error="REMOTE_WORKSPACE_INACTIVE",
            )

        # Capability check via worker connection metadata if available
        conn = await get_remote_worker_registry().get(state.login)
        if conn is None:
            return McpServerListRow(
                name=server.name,
                location="remote",
                attach_on=getattr(server, "attach_on", "remote_use"),
                attached=False,
                error="REMOTE_WORKER_OFFLINE",
            )
        meta = getattr(conn, "hello_meta", None) or {}
        if meta and not worker_supports_remote_mcp(
            protocol_version=meta.get("protocol_version"),
            capabilities=meta.get("capabilities"),
        ):
            return McpServerListRow(
                name=server.name,
                location="remote",
                attach_on=getattr(server, "attach_on", "remote_use"),
                attached=False,
                error="REMOTE_PROTOCOL_TOO_OLD",
            )

        registry = get_remote_worker_registry()
        ensure = await registry.mcp_ensure(
            login=state.login,
            session_id=session_id,
            servers=[server.name],
            timeout_seconds=float(server.init_timeout_seconds or 30),
        )
        status = ensure.servers[0] if ensure.servers else None
        if status is None or not status.ok:
            return McpServerListRow(
                name=server.name,
                location="remote",
                attach_on=getattr(server, "attach_on", "remote_use"),
                attached=False,
                error=(status.error if status else "MCP_ENSURE_FAILED"),
            )

        listed = await registry.mcp_list_tools(
            login=state.login,
            session_id=session_id,
            server_name=server.name,
            refresh=False,
            timeout_seconds=float(server.init_timeout_seconds or 30),
        )
        if listed.error:
            return McpServerListRow(
                name=server.name,
                location="remote",
                attach_on=getattr(server, "attach_on", "remote_use"),
                attached=False,
                error=listed.error,
            )

        existing_names = self._all_tool_names(agent)
        proxies: List[BaseTool] = []
        for meta_tool in listed.tools:
            prefix = server.tool_name_prefix or server.name
            local_name = mcp_openai_safe_local_name(str(prefix), meta_tool.name)
            if local_name in existing_names:
                local_name = mcp_openai_safe_local_name(f"r__{prefix}", meta_tool.name)
            # de-dup further
            base = local_name
            n = 2
            while local_name in existing_names:
                local_name = f"{base}__{n}"
                n += 1
            existing_names.add(local_name)
            proxies.append(
                RemoteMCPProxyTool(
                    local_name=local_name,
                    server_name=server.name,
                    remote_name=meta_tool.name,
                    description=meta_tool.description or "远程 MCP 工具",
                    input_schema=meta_tool.input_schema or {},
                    login=state.login,
                    session_id=session_id,
                    call_timeout_seconds=float(server.call_timeout_seconds or 30),
                )
            )

        apply = getattr(agent, "_apply_mcp_proxy_tools", None)
        if apply:
            apply(proxies)
        else:
            self._register_tools(agent, proxies)
        names = [p.name for p in proxies]
        self._overlay_map(agent)[server.name] = set(names)
        return McpServerListRow(
            name=server.name,
            location="remote",
            attach_on=getattr(server, "attach_on", "remote_use"),
            attached=True,
            tool_names=names,
        )

    async def _shutdown_remote_server(
        self, agent: Any, session_id: str, server_name: str
    ) -> None:
        try:
            from agent_core.remote.workspace_state import get_remote_workspace_state
            from agent_core.remote.worker_registry import get_remote_worker_registry

            state = get_remote_workspace_state(session_id)
            if state is None:
                return
            await get_remote_worker_registry().mcp_shutdown(
                login=state.login,
                session_id=session_id,
                server_name=server_name,
            )
        except Exception:
            logger.debug(
                "remote mcp shutdown skipped session=%s server=%s",
                session_id,
                server_name,
                exc_info=True,
            )

    def _find_server(
        self, cfg: Config, server_name: str
    ) -> Optional[MCPServerConfig]:
        if not getattr(cfg, "mcp", None):
            return None
        key = (server_name or "").strip()
        for server in cfg.mcp.servers:
            if server.name == key:
                return server
        return None

    def _overlay_map(self, agent: Any) -> Dict[str, Set[str]]:
        overlay = getattr(agent, "_mcp_overlay_tools", None)
        if overlay is None:
            overlay = {}
            agent._mcp_overlay_tools = overlay
        return overlay

    def _all_tool_names(self, agent: Any) -> Set[str]:
        names: Set[str] = set()
        for reg_name in ("_tool_registry", "_tool_catalog"):
            reg = getattr(agent, reg_name, None)
            if reg is None:
                continue
            try:
                _, mapping = reg.list_tools()
                names.update(mapping.keys())
            except Exception:
                pass
        return names

    def _register_tools(self, agent: Any, tools: List[BaseTool]) -> None:
        for reg_name in ("_tool_registry", "_tool_catalog"):
            reg = getattr(agent, reg_name, None)
            if reg is None:
                continue
            if hasattr(reg, "update_tools"):
                reg.update_tools(tools)

    def _unregister_tools(self, agent: Any, tool_names: List[str]) -> None:
        working = getattr(agent, "_working_set", None)
        for name in tool_names:
            if working is not None and hasattr(working, "unpin"):
                try:
                    working.unpin(name)
                except Exception:
                    pass
            for reg_name in ("_tool_registry", "_tool_catalog"):
                reg = getattr(agent, reg_name, None)
                if reg is None:
                    continue
                if hasattr(reg, "unregister"):
                    try:
                        reg.unregister(name)
                    except Exception:
                        pass
            unregister = getattr(agent, "unregister_tool", None)
            if unregister:
                try:
                    unregister(name)
                except Exception:
                    pass


_OVERLAY = McpSessionOverlay()


def get_mcp_session_overlay() -> McpSessionOverlay:
    return _OVERLAY
