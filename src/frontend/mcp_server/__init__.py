"""
macchiatoBot MCP Server。

将现有本地工具以 MCP 标准协议暴露，供外部 MCP Client 调用。
"""

from .server import ScheduleToolsMCPServer, run_stdio_server

__all__ = [
    "ScheduleToolsMCPServer",
    "run_stdio_server",
]
