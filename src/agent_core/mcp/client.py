"""
MCP 客户端管理器。

管理多个 MCP Server 连接，并提供远程工具调用能力。
外部 stdio MCP（如 Tavily、Discourse）通过进程内共享池复用子进程；本地 mcp_server.py 仍独占连接。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

from agent_core.config import MCPConfig, MCPServerConfig
from agent_core.tools.base import ToolResult

from .pool import get_shared_mcp_pool, should_pool_stdio_server
from .proxy_tool import MCPProxyTool
from .stdio_transport import MCPServerRuntime, connect_stdio_mcp_with_retries

logger = logging.getLogger(__name__)


class MCPClientManager:
    """MCP 客户端连接与工具调用管理。"""

    def __init__(self, config: MCPConfig):
        self._config = config
        self._exit_stack = AsyncExitStack()
        self._server_stacks: List[AsyncExitStack] = []
        self._servers: Dict[str, MCPServerRuntime] = {}
        self._proxy_tools: List[MCPProxyTool] = []
        self._connected = False
        self._pooled_release_keys: List[str] = []
        self._pool_key_by_server: Dict[str, str] = {}

    async def connect(self) -> None:
        """连接所有启用的 MCP Server，并构建代理工具。"""
        if self._connected:
            return

        try:
            from mcp import ClientSession, StdioServerParameters  # noqa: F401
            from mcp.client.stdio import stdio_client  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "未安装 mcp 依赖，请先执行: source init.sh（或 uv sync --all-groups）"
            ) from e

        pool = get_shared_mcp_pool()

        for server in self._config.servers:
            if not server.enabled:
                continue
            if server.transport != "stdio":
                continue

            if should_pool_stdio_server(server):
                pooled = await pool.acquire(server)
                if pooled is None:
                    continue
                pool_key, runtime, tool_metas = pooled
                self._servers[server.name] = runtime
                self._pool_key_by_server[server.name] = pool_key
                self._pooled_release_keys.append(pool_key)

                for remote_name, description, input_schema in tool_metas:
                    local_prefix = server.tool_name_prefix or server.name
                    local_name = f"{local_prefix}.{remote_name}"
                    if any(t.name == local_name for t in self._proxy_tools):
                        raise ValueError(f"MCP 工具名冲突: {local_name}")

                    self._proxy_tools.append(
                        MCPProxyTool(
                            manager=self,
                            local_name=local_name,
                            server_name=server.name,
                            remote_name=remote_name,
                            description=description,
                            input_schema=input_schema,
                        )
                    )
                continue

            connected = await connect_stdio_mcp_with_retries(server)
            if connected is None:
                continue
            attempt_stack, runtime = connected
            self._servers[server.name] = runtime

            try:
                tool_resp = await asyncio.wait_for(
                    runtime.session.list_tools(),
                    timeout=server.init_timeout_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "MCP server %s list_tools failed (skipping): %s (%s)",
                    server.name,
                    type(exc).__name__,
                    exc,
                )
                await attempt_stack.aclose()
                del self._servers[server.name]
                continue

            tools = getattr(tool_resp, "tools", []) or []
            for tool in tools:
                remote_name = getattr(tool, "name", "")
                if not remote_name:
                    continue
                local_prefix = server.tool_name_prefix or server.name
                local_name = f"{local_prefix}.{remote_name}"
                if any(t.name == local_name for t in self._proxy_tools):
                    await attempt_stack.aclose()
                    del self._servers[server.name]
                    raise ValueError(f"MCP 工具名冲突: {local_name}")

                self._proxy_tools.append(
                    MCPProxyTool(
                        manager=self,
                        local_name=local_name,
                        server_name=server.name,
                        remote_name=remote_name,
                        description=getattr(tool, "description", "") or "MCP 远程工具",
                        input_schema=getattr(tool, "inputSchema", None)
                        or getattr(tool, "input_schema", None)
                        or {"type": "object", "properties": {}},
                    )
                )

            self._server_stacks.append(attempt_stack)

        self._connected = True
        if self._servers:
            logger.info(
                "MCP connect done: %d server(s) ready, %d tool(s)",
                len(self._servers),
                len(self._proxy_tools),
            )
        else:
            logger.warning("MCP connect done: no servers available (all failed or disabled)")

    def get_proxy_tools(self) -> List[MCPProxyTool]:
        """获取已构建的 MCP 代理工具列表。"""
        return list(self._proxy_tools)

    async def call_tool(
        self,
        server_name: str,
        remote_tool_name: str,
        arguments: Dict[str, Any],
    ) -> ToolResult:
        """调用指定 MCP Server 的远程工具。"""
        runtime = self._servers.get(server_name)
        if runtime is None:
            return ToolResult(
                success=False,
                error="MCP_SERVER_NOT_FOUND",
                message=f"MCP Server 不存在或未连接: {server_name}",
            )

        timeout_seconds = (
            runtime.config.call_timeout_seconds or self._config.call_timeout_seconds
        )
        pool_key = self._pool_key_by_server.get(server_name)

        async def _invoke():
            return await runtime.session.call_tool(
                remote_tool_name, arguments=arguments
            )

        try:
            pool = get_shared_mcp_pool()
            if pool_key:
                async with pool.locked_session_call(pool_key):
                    result = await asyncio.wait_for(
                        _invoke(),
                        timeout=timeout_seconds,
                    )
            else:
                result = await asyncio.wait_for(
                    _invoke(),
                    timeout=timeout_seconds,
                )
            return self._convert_call_result(result)
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error="MCP_TOOL_TIMEOUT",
                message=f"MCP 工具调用超时: {server_name}.{remote_tool_name}",
                metadata={"timeout_seconds": timeout_seconds},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error="MCP_TOOL_CALL_FAILED",
                message=f"MCP 工具调用失败: {server_name}.{remote_tool_name}: {str(e)}",
            )

    async def close(self) -> None:
        """关闭所有 MCP 连接。"""
        if not self._connected:
            return
        pool = get_shared_mcp_pool()
        for key in self._pooled_release_keys:
            await pool.release(key)
        self._pooled_release_keys.clear()
        self._pool_key_by_server.clear()

        for stack in self._server_stacks:
            await stack.aclose()
        self._server_stacks.clear()
        await self._exit_stack.aclose()
        self._servers.clear()
        self._proxy_tools.clear()
        self._connected = False

    def _convert_call_result(self, call_result: Any) -> ToolResult:
        """将 MCP 工具调用结果转换为本地 ToolResult。"""
        is_error = bool(
            getattr(call_result, "isError", False)
            or getattr(call_result, "is_error", False)
        )
        content = getattr(call_result, "content", None)
        structured = getattr(call_result, "structuredContent", None) or getattr(
            call_result, "structured_content", None
        )

        text_parts: List[str] = []
        serialized_content = []
        if isinstance(content, list):
            for block in content:
                serialized = self._serialize(block)
                serialized_content.append(serialized)
                if isinstance(serialized, dict) and "text" in serialized:
                    text_parts.append(str(serialized.get("text", "")))
                elif isinstance(serialized, str):
                    text_parts.append(serialized)

        data: Any = structured if structured is not None else serialized_content
        message = "\n".join([t for t in text_parts if t]).strip()
        if not message:
            message = "MCP 工具执行失败" if is_error else "MCP 工具执行成功"

        return ToolResult(
            success=not is_error,
            data=data,
            message=message,
            error="MCP_TOOL_ERROR" if is_error else None,
        )

    def _serialize(self, obj: Any) -> Any:
        """尽量将对象转换为可 JSON 序列化结构。"""
        if obj is None:
            return None
        if isinstance(obj, (str, int, float, bool, list, dict)):
            return obj
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "dict"):
            return obj.dict()
        try:
            return json.loads(json.dumps(obj, default=str))
        except Exception:
            return str(obj)
