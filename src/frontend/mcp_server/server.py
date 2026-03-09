"""
Schedule Agent 工具 MCP Server。

复用现有 BaseTool/ToolRegistry，将本地工具通过 MCP 协议（stdio）暴露。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from agent_core.config import Config, get_config
from agent_core.tools import ToolResult
from system.tools import VersionedToolRegistry, build_tool_registry


class ScheduleToolsMCPServer:
    """将 system 层工具注册表以 MCP Server 形式暴露。"""

    def __init__(
        self,
        config: Optional[Config] = None,
        tools: Optional[List[Any]] = None,
        server_name: str = "schedule-agent-tools",
        server_version: str = "0.1.0",
    ):
        self._config = config or get_config()
        # 基于 CoreProfile 默认 full 权限构建系统级工具注册表
        from agent_core.kernel_interface import CoreProfile

        base_profile = CoreProfile.default_full(
            frontend_id="cli",
            dialog_window_id="root",
        )
        registry: VersionedToolRegistry = build_tool_registry(
            profile=base_profile,
            config=self._config,
        )
        # 允许调用方额外注入自定义工具
        if tools:
            from agent_core.tools.base import BaseTool as _BaseTool  # 仅作类型检查

            for tool in tools:
                if isinstance(tool, _BaseTool) and not registry.has(tool.name):
                    registry.register(tool)

        self._registry = registry

        self._server_name = server_name
        self._server_version = server_version
        self._server = Server(server_name)
        self._register_handlers()

    @property
    def server(self) -> Server:
        """返回底层 MCP Server 对象。"""
        return self._server

    def _register_handlers(self) -> None:
        @self._server.list_tools()
        async def _list_tools() -> List[types.Tool]:
            return self.list_tools()

        @self._server.call_tool(validate_input=False)
        async def _call_tool(
            name: str,
            arguments: Dict[str, Any] | None,
        ) -> types.CallToolResult:
            return await self.call_tool(name=name, arguments=arguments or {})

    def list_tools(self) -> List[types.Tool]:
        """返回 MCP Tool 列表。"""
        result: List[types.Tool] = []
        _version, tools = self._registry.list_tools()
        for tool in tools.values():
            definition = tool.get_definition()
            result.append(
                types.Tool(
                    name=definition.name,
                    description=definition._build_description(),
                    inputSchema=self._build_input_schema(definition.parameters),
                )
            )
        return result

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> types.CallToolResult:
        """执行指定工具并转换为 MCP 标准返回。"""
        result = await self._registry.execute(name, **(arguments or {}))
        payload = self._to_payload(result)
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structuredContent=payload,
            isError=not result.success,
        )

    async def run_stdio(self) -> None:
        """以 stdio 方式运行 MCP Server。"""
        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=self._server_name,
                    server_version=self._server_version,
                    capabilities=self._server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )

    def _build_input_schema(self, parameters: List[Any]) -> Dict[str, Any]:
        properties: Dict[str, Any] = {}
        required: List[str] = []
        for param in parameters:
            schema: Dict[str, Any] = {
                "type": param.type,
                "description": param.description,
            }
            if param.enum:
                schema["enum"] = param.enum
            if param.default is not None:
                schema["default"] = param.default
            properties[param.name] = schema
            if param.required:
                required.append(param.name)
        input_schema: Dict[str, Any] = {
            "type": "object",
            "properties": properties,
        }
        if required:
            input_schema["required"] = required
        return input_schema

    def _to_payload(self, result: ToolResult) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "success": result.success,
            "message": result.message,
        }
        if result.data is not None:
            payload["data"] = self._serialize(result.data)
        if result.error:
            payload["error"] = result.error
        if result.metadata:
            payload["metadata"] = self._serialize(result.metadata)
        return payload

    def _serialize(self, obj: Any) -> Any:
        if obj is None:
            return None
        if isinstance(obj, (str, int, float, bool, list, dict)):
            return obj
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "dict"):
            return obj.dict()
        return str(obj)


async def run_stdio_server(
    config: Optional[Config] = None,
    tools: Optional[List[BaseTool]] = None,
) -> None:
    """便捷函数：使用默认配置运行 MCP stdio server。"""
    server = ScheduleToolsMCPServer(config=config, tools=tools)
    await server.run_stdio()
