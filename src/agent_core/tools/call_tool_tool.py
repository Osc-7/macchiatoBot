"""
通用工具执行器。

用于在 search_tools 发现工具后，通过 name + arguments 统一执行。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from .versioned_registry import VersionedToolRegistry

# 由 AgentCore 注入，用于对内层工具名做与 Kernel 一致的 CoreProfile 校验
ProfileGetter = Optional[Callable[[], Any]]


class CallToolTool(BaseTool):
    """通过工具名动态调用工具。"""

    def __init__(
        self,
        registry: VersionedToolRegistry,
        profile_getter: ProfileGetter = None,
    ):
        self._registry = registry
        self._profile_getter = profile_getter

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
            tags=["工具", "执行"],
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

        if self._profile_getter is not None:
            profile = self._profile_getter()
            if profile is not None and not profile.is_tool_allowed(name):
                return ToolResult(
                    success=False,
                    error="PERMISSION_DENIED",
                    message=(
                        f"权限拒绝：工具 '{name}' 不在该 Core 的权限范围内"
                    ),
                )

        # 透传 __execution_context__，否则内层工具（如 create_subagent）无法获取
        # 调用方的 session_id，导致 parent_session_id 为空。
        exec_ctx = kwargs.get("__execution_context__")
        if exec_ctx is not None and "__execution_context__" not in arguments:
            arguments = {**arguments, "__execution_context__": exec_ctx}

        return await self._registry.execute(name, **arguments)
