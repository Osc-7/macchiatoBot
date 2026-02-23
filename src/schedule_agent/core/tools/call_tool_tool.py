"""
通用工具执行器。

用于在 search_tools 发现工具后，通过 name + arguments 统一执行。
"""

from __future__ import annotations

from typing import Any, Dict

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from .versioned_registry import VersionedToolRegistry


class CallToolTool(BaseTool):
    """通过工具名动态调用工具。"""

    def __init__(self, registry: VersionedToolRegistry):
        self._registry = registry

    @property
    def name(self) -> str:
        return "call_tool"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="按工具名执行工具。通常先通过 search_tools 查询工具，再调用此工具执行。",
            parameters=[
                ToolParameter(
                    name="name",
                    type="string",
                    description="目标工具名称，例如 add_event、mcp_local.get_tasks",
                    required=True,
                ),
                ToolParameter(
                    name="arguments",
                    type="object",
                    description="目标工具参数对象（JSON object）",
                    required=False,
                    default={},
                ),
            ],
            usage_notes=[
                "name 必须是已注册的工具名称。",
                "arguments 需符合目标工具参数定义。",
            ],
            tags=['工具', '执行'],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        name = str(kwargs.get("name", "")).strip()
        if not name:
            return ToolResult(
                success=False,
                error="INVALID_ARGUMENTS",
                message="name 不能为空",
            )

        arguments = kwargs.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return ToolResult(
                success=False,
                error="INVALID_ARGUMENTS",
                message="arguments 必须是对象",
            )

        if not self._registry.has(name):
            return ToolResult(
                success=False,
                error="TOOL_NOT_FOUND",
                message=f"工具 '{name}' 不存在",
            )

        return await self._registry.execute(name, **arguments)
