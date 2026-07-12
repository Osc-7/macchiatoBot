"""Remote-workspace MCP proxy tool (daemon-side)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent_core.mcp.proxy_tool import _schema_to_parameters
from agent_core.tools.base import BaseTool, ToolDefinition, ToolResult


class RemoteMCPProxyTool(BaseTool):
    """Proxy for an MCP tool that executes on a remote worker."""

    def __init__(
        self,
        *,
        local_name: str,
        server_name: str,
        remote_name: str,
        description: str,
        input_schema: Dict[str, Any],
        login: str,
        session_id: str,
        call_timeout_seconds: Optional[float] = None,
    ) -> None:
        self._local_name = local_name
        self._server_name = server_name
        self._remote_name = remote_name
        self._description = description or "远程 MCP 工具"
        self._input_schema = input_schema or {"type": "object", "properties": {}}
        self._login = login
        self._session_id = session_id
        self._call_timeout_seconds = call_timeout_seconds

    @property
    def name(self) -> str:
        return self._local_name

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def mcp_location(self) -> str:
        return "remote"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self._local_name,
            description=self._description,
            parameters=_schema_to_parameters(self._input_schema),
            usage_notes=[
                f"该工具来自远程 MCP Server: {self._server_name}",
                f"远程工具名: {self._remote_name}",
                f"远程 login: {self._login}",
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        from agent_core.remote.worker_registry import get_remote_worker_registry

        try:
            result = await get_remote_worker_registry().mcp_call_tool(
                login=self._login,
                session_id=self._session_id,
                server_name=self._server_name,
                tool_name=self._remote_name,
                arguments=kwargs,
                timeout_seconds=self._call_timeout_seconds,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error="MCP_REMOTE_CALL_FAILED",
                message=f"远程 MCP 调用失败: {self._server_name}.{self._remote_name}: {exc}",
            )

        if result.error and result.is_error:
            return ToolResult(
                success=False,
                error=result.error,
                message=result.error,
                data={"content": result.content},
            )
        # Flatten text content for message
        texts: List[str] = []
        for block in result.content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(str(block.get("text") or ""))
                elif "text" in block:
                    texts.append(str(block.get("text") or ""))
        message = "\n".join(t for t in texts if t).strip() or (
            "远程 MCP 调用完成" if not result.is_error else (result.error or "error")
        )
        return ToolResult(
            success=not result.is_error,
            data={
                "content": result.content,
                "structured_content": result.structured_content,
                "mcp_server": self._server_name,
                "mcp_location": "remote",
            },
            message=message,
            error=result.error if result.is_error else None,
        )
