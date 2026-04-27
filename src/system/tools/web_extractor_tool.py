"""
网页抓取工具（Tavily MCP 适配）。

通过 MCP 代理的 Tavily extract 工具访问指定 URL 并提取内容。
"""

from typing import List

from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from agent_core.tools.versioned_registry import VersionedToolRegistry


class WebExtractorTool(BaseTool):
    """
    网页抓取工具

    访问指定 URL 并提取网页内容。
    不在工具内部进行二次 LLM 总结，直接返回 Tavily 的结构化结果。
    """

    def __init__(self, registry: VersionedToolRegistry):
        self._registry = registry

    @property
    def name(self) -> str:
        return "extract_web_content"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="extract_web_content",
            description="""访问指定网页 URL 并提取其内容。

当用户想要：
- 查看某个网页的内容
- 总结网页或文档
- 提取网页中的关键信息
- 分析网页内容

工具会自动：
- 调用 Tavily extract 访问 URL
- 提取网页正文和结构化字段
- 返回结构化结果（不做额外 LLM 汇总）

注意：此工具仅支持公开可访问的网页 URL。""",
            parameters=[
                ToolParameter(
                    name="url",
                    type="string",
                    description="要访问的网页 URL（必须以 http:// 或 https:// 开头）",
                    required=True,
                ),
                ToolParameter(
                    name="query",
                    type="string",
                    description="可选的查询或任务描述，说明你想从网页中获取什么信息（例如：'总结主要内容'、'提取关键数据'）",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "查看并总结网页内容",
                    "params": {
                        "url": "https://example.com/article",
                        "query": "总结这篇文章的主要内容",
                    },
                },
                {
                    "description": "提取网页关键信息",
                    "params": {
                        "url": "https://docs.example.com/api",
                        "query": "提取 API 文档中的关键接口说明",
                    },
                },
                {
                    "description": "简单查看网页",
                    "params": {
                        "url": "https://example.com",
                    },
                },
            ],
            usage_notes=[
                "URL 必须是完整的、可公开访问的网页地址",
                "如果网页需要登录或验证，可能无法访问",
                "工具会返回 Tavily 的原始/结构化抽取结果，不会在工具内再次调用 LLM 总结",
                "如果指定了 query，会作为抽取意图透传给 Tavily（若该参数被支持）",
                "查询火车/航班时刻时，务必在 query 中写明出发地、目的地（如「厦门到上海 G260 时刻表」），否则可能返回同名车次/航班的其它线路信息。",
            ],
            tags=["网络", "抓取"],
        )

    def _resolve_remote_tool_name(self) -> str | None:
        names = self._registry.list_names()
        candidates = ["tavily-extract", "extract", "tavily_extract"]
        for full_name in names:
            if full_name in candidates:
                return full_name
            for candidate in candidates:
                if full_name.endswith(f".{candidate}") or full_name.endswith(
                    f"__{candidate}"
                ):
                    return full_name
        return None

    async def execute(self, **kwargs) -> ToolResult:
        """
        执行网页抓取。

        Args:
            url: 网页 URL
            query: 可选的查询描述

        Returns:
            工具执行结果
        """
        url = kwargs.get("url")
        query = kwargs.get("query", "")

        if not url:
            return ToolResult(
                success=False,
                error="MISSING_URL",
                message="缺少必需的参数：url",
            )

        if not url.startswith(("http://", "https://")):
            return ToolResult(
                success=False,
                error="INVALID_URL",
                message=f"URL 格式错误，必须以 http:// 或 https:// 开头: {url}",
            )

        remote_name = self._resolve_remote_tool_name()
        if not remote_name:
            return ToolResult(
                success=False,
                error="TAVILY_EXTRACT_NOT_AVAILABLE",
                message="未找到 Tavily extract 工具，请检查 MCP 配置与连接状态",
            )

        payload_candidates: List[dict] = []
        payload = {"urls": [url]}
        if query:
            payload["query"] = query
        payload_candidates.append(payload)
        payload_candidates.append(
            {"url": url, "query": query} if query else {"url": url}
        )

        last_result: ToolResult | None = None
        for candidate_payload in payload_candidates:
            result = await self._registry.execute(remote_name, **candidate_payload)
            last_result = result
            if result.success:
                return ToolResult(
                    success=True,
                    data={"url": url, "extract_result": result.data},
                    message=f"成功提取网页内容：{url}",
                    metadata={"source_tool": remote_name},
                )

        return ToolResult(
            success=False,
            error=last_result.error if last_result else "EXTRACTION_ERROR",
            message=last_result.message if last_result else "网页抓取失败",
            metadata={"source_tool": remote_name},
        )
