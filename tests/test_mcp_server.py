"""
MCP Server 适配层测试。
"""

import json
from pathlib import Path

import pytest

from agent_core.config import AgentConfig, Config, LLMConfig, MemoryConfig
from system.tools.parse_time import ParseTimeTool
from frontend.mcp_server import ScheduleToolsMCPServer


def _mcp_test_config(tmp_path: Path) -> Config:
    return Config(
        llm=LLMConfig(api_key="k", model="test-model"),
        memory=MemoryConfig(memory_base_dir=str(tmp_path / "memory")),
        agent=AgentConfig(),
    )


class TestScheduleToolsMCPServer:
    """测试本地工具 MCP Server 封装。"""

    def test_list_tools_contains_registered_tool(self, tmp_path: Path):
        server = ScheduleToolsMCPServer(
            config=_mcp_test_config(tmp_path), tools=[ParseTimeTool()]
        )
        tools = server.list_tools()
        names = [tool.name for tool in tools]
        assert "parse_time" in names

    @pytest.mark.asyncio
    async def test_call_tool_success(self, tmp_path: Path):
        server = ScheduleToolsMCPServer(
            config=_mcp_test_config(tmp_path), tools=[ParseTimeTool()]
        )
        result = await server.call_tool(
            name="parse_time",
            arguments={"time_text": "明天下午3点"},
        )
        assert result.isError is False
        assert isinstance(result.structuredContent, dict)
        assert result.structuredContent.get("success") is True
        assert len(result.content) == 1
        payload = json.loads(result.content[0].text)
        assert payload["success"] is True

    @pytest.mark.asyncio
    async def test_call_tool_not_found(self, tmp_path: Path):
        server = ScheduleToolsMCPServer(
            config=_mcp_test_config(tmp_path), tools=[ParseTimeTool()]
        )
        result = await server.call_tool(name="nonexistent_tool", arguments={})
        assert result.isError is True
        assert result.structuredContent.get("success") is False
        assert result.structuredContent.get("error") == "TOOL_NOT_FOUND"
