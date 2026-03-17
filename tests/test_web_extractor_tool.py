"""
网页抓取工具测试（Tavily MCP 适配版本）
"""

import pytest

from agent_core.tools import VersionedToolRegistry
from system.tools.web_extractor_tool import WebExtractorTool


class TestWebExtractorTool:
    """测试 WebExtractorTool"""

    def test_tool_initialization(self):
        """测试工具初始化"""
        registry = VersionedToolRegistry()
        tool = WebExtractorTool(registry=registry)
        assert tool.name == "extract_web_content"

    def test_get_definition(self):
        """测试工具定义"""
        registry = VersionedToolRegistry()
        tool = WebExtractorTool(registry=registry)
        definition = tool.get_definition()

        assert definition.name == "extract_web_content"
        assert len(definition.parameters) == 2
        assert definition.parameters[0].name == "url"
        assert definition.parameters[1].name == "query"

    @pytest.mark.asyncio
    async def test_execute_missing_url(self):
        """测试缺少 URL 参数"""
        registry = VersionedToolRegistry()
        tool = WebExtractorTool(registry=registry)
        result = await tool.execute()

        assert result.success is False
        assert result.error == "MISSING_URL"

    @pytest.mark.asyncio
    async def test_execute_invalid_url(self):
        """测试无效 URL"""
        registry = VersionedToolRegistry()
        tool = WebExtractorTool(registry=registry)
        result = await tool.execute(url="invalid-url")

        assert result.success is False
        assert result.error == "INVALID_URL"

    @pytest.mark.asyncio
    async def test_execute_remote_tool_not_available(self):
        """未配置 Tavily extract 时返回明确错误"""
        registry = VersionedToolRegistry()
        tool = WebExtractorTool(registry=registry)

        result = await tool.execute(url="https://example.com")

        assert result.success is False
        assert result.error == "TAVILY_EXTRACT_NOT_AVAILABLE"

    @pytest.mark.asyncio
    async def test_execute_success_with_tavily_extract(self):
        """成功调用 Tavily extract 并返回结构化数据"""
        registry = VersionedToolRegistry()

        class FakeExtractTool:
            @property
            def name(self):
                return "tavily.extract"

            def to_openai_tool(self):
                return {
                    "type": "function",
                    "function": {
                        "name": "tavily.extract",
                        "description": "",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        },
                    },
                }

            async def execute(self, **kwargs):
                assert "urls" in kwargs or "url" in kwargs
                return type(
                    "R",
                    (),
                    {
                        "success": True,
                        "data": {
                            "results": [
                                {"url": "https://example.com", "content": "网页正文"}
                            ]
                        },
                        "message": "ok",
                        "error": None,
                        "metadata": {},
                    },
                )()

        registry.register(FakeExtractTool())
        tool = WebExtractorTool(registry=registry)

        result = await tool.execute(url="https://example.com", query="提取关键数据")

        assert result.success is True
        assert result.data["url"] == "https://example.com"
        assert "extract_result" in result.data
        assert result.metadata["source_tool"] == "tavily.extract"
