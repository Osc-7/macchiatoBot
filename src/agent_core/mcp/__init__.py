"""
MCP 客户端适配层。

负责连接 MCP Server，并将远端工具包装为本地 BaseTool。
"""

from .client import MCPClientManager, mcp_openai_safe_local_name
from .proxy_tool import MCPProxyTool
from .remote_proxy_tool import RemoteMCPProxyTool
from .session_overlay import McpSessionOverlay, get_mcp_session_overlay

__all__ = [
    "MCPClientManager",
    "MCPProxyTool",
    "RemoteMCPProxyTool",
    "McpSessionOverlay",
    "get_mcp_session_overlay",
    "mcp_openai_safe_local_name",
]
