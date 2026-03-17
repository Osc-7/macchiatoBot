"""
网页搜索工具测试（Tavily MCP 适配版本）
"""

import pytest

from agent_core.tools import VersionedToolRegistry
from system.tools.web_search_tool import WebSearchTool


class _FakeSearchTool:
    @property
    def name(self):
        return "tavily.search"

    def to_openai_tool(self):
        return {
            "type": "function",
            "function": {
                "name": "tavily.search",
                "description": "",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }

    async def execute(self, **kwargs):
        return type(
            "R",
            (),
            {
                "success": True,
                "data": {
                    "results": [
                        {
                            "title": "Qwen 新模型发布",
                            "url": "https://example.com/news",
                            "content": "摘要内容",
                        }
                    ]
                },
                "message": "ok",
                "error": None,
                "metadata": {},
            },
        )()


class _FakeTimeoutSearchTool:
    @property
    def name(self):
        return "tavily.search"

    def to_openai_tool(self):
        return {
            "type": "function",
            "function": {
                "name": "tavily.search",
                "description": "",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }

    async def execute(self, **kwargs):
        return type(
            "R",
            (),
            {
                "success": False,
                "data": None,
                "message": "MCP 工具调用超时: tavily.search",
                "error": "MCP_TOOL_TIMEOUT",
                "metadata": {"timeout_seconds": 45},
            },
        )()


def test_tool_name():
    registry = VersionedToolRegistry()
    tool = WebSearchTool(registry=registry)
    assert tool.name == "web_search"


@pytest.mark.asyncio
async def test_missing_query():
    registry = VersionedToolRegistry()
    tool = WebSearchTool(registry=registry)

    result = await tool.execute()
    assert result.success is False
    assert result.error == "MISSING_QUERY"


@pytest.mark.asyncio
async def test_search_tool_not_available():
    registry = VersionedToolRegistry()
    tool = WebSearchTool(registry=registry)

    result = await tool.execute(query="Qwen 最新模型")
    assert result.success is False
    assert result.error == "TAVILY_SEARCH_NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_search_success():
    registry = VersionedToolRegistry()
    registry.register(_FakeSearchTool())
    tool = WebSearchTool(registry=registry)

    result = await tool.execute(
        query="Qwen 最新模型",
        max_results=5,
        search_depth="advanced",
        include_domains=["help.aliyun.com"],
    )
    assert result.success is True
    assert result.data["query"] == "Qwen 最新模型"
    assert "search_result" in result.data
    assert result.metadata["source_tool"] == "tavily.search"


@pytest.mark.asyncio
async def test_search_timeout_fallback():
    registry = VersionedToolRegistry()
    registry.register(_FakeTimeoutSearchTool())
    tool = WebSearchTool(registry=registry)

    result = await tool.execute(query="实时天气")
    assert result.success is False
    assert result.error == "MCP_TOOL_TIMEOUT"
    assert "超时" in result.message
