"""
网页搜索工具（Tavily MCP 适配）。

通过 MCP 代理的 Tavily search 工具按关键词搜索网络信息。
"""

from typing import Optional

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from .versioned_registry import VersionedToolRegistry


class WebSearchTool(BaseTool):
    """
    独立的联网搜索工具。

    工具只返回 Tavily 的结构化搜索结果，不在工具内部做二次 LLM 汇总。
    """

    def __init__(self, registry: VersionedToolRegistry):
        self._registry = registry

    @property
    def name(self) -> str:
        return "web_search"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="web_search",
            description="""按关键词搜索网络公开信息并返回结构化结果。

当用户想要：
- 查询最新新闻、公告、技术动态
- 查找某个主题的资料来源链接
- 获取实时公开事实（天气、价格、政策更新等）

工具会自动：
- 调用 Tavily search 进行网络搜索
- 返回结构化结果（标题、链接、摘要等）

注意：此工具返回原始搜索结果，不在工具内做总结。""",
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description="搜索关键词或问题描述",
                    required=True,
                ),
                ToolParameter(
                    name="max_results",
                    type="integer",
                    description="可选，返回结果数量上限",
                    required=False,
                ),
                ToolParameter(
                    name="search_depth",
                    type="string",
                    description="可选，搜索深度（如 basic/advanced）",
                    required=False,
                ),
                ToolParameter(
                    name="include_domains",
                    type="array",
                    description="可选，仅从指定域名搜索（字符串数组）",
                    required=False,
                ),
            ],
            usage_notes=[
                "结果为 Tavily 结构化搜索数据，后续回答由主对话 LLM基于 tool_result 组织",
                "若需要读取某个具体网页正文，优先使用 extract_web_content(url=...)",
            ],
            tags=["网络", "搜索"],
        )

    def _resolve_remote_tool_name(self) -> Optional[str]:
        names = self._registry.list_names()
        candidates = ["tavily-search", "search", "tavily_search"]
        for full_name in names:
            if full_name in candidates:
                return full_name
            for candidate in candidates:
                if full_name.endswith(f".{candidate}"):
                    return full_name
        return None

    def _looks_like_chat_history_query(self, query: str) -> bool:
        """
        粗略判断 query 是否更像是在查「自己和助手的历史对话」，而不是互联网公开信息。
        命中时应优先使用 chat_search/chat_context/chat_scroll 等聊天历史工具，而非联网搜索。
        """
        q = (query or "").strip()
        if not q:
            return False

        q_lower = q.lower()

        # 明确提到聊天/对话历史的关键词
        zh_keywords = [
            "聊天记录",
            "历史对话",
            "历史聊天",
            "对话历史",
            "之前聊过",
            "之前说过",
            "上次说的",
            "刚才说的",
            "刚刚说的",
        ]
        en_keywords = [
            "chat history",
            "conversation history",
            "previous chat",
            "earlier conversation",
        ]
        if any(k in q for k in zh_keywords):
            return True
        if any(k in q_lower for k in en_keywords):
            return True

        # 含「你」「我」且带时间指代，大概率是在指代双方之前的对话
        if "你" in q and "我" in q and any(t in q for t in ["之前", "刚才", "刚刚", "上次"]):
            return True

        return False

    async def execute(self, **kwargs) -> ToolResult:
        query = kwargs.get("query")
        max_results = kwargs.get("max_results")
        search_depth = kwargs.get("search_depth")
        include_domains = kwargs.get("include_domains")

        if not query:
            return ToolResult(
                success=False,
                error="MISSING_QUERY",
                message="缺少必需的参数：query",
            )

        # 防御性保护：当 query 明显是在查自己和助手的历史对话时，提示改用聊天历史工具。
        if isinstance(query, str) and self._looks_like_chat_history_query(query):
            return ToolResult(
                success=False,
                error="SHOULD_USE_CHAT_HISTORY_TOOLS",
                message=(
                    "当前问题看起来是在查找「你和用户自己的历史对话」或之前说过的内容，"
                    "不适合使用联网搜索 web_search。\n\n"
                    "请改用聊天历史相关工具：\n"
                    "- chat_search：按关键词在 ChatHistoryDB 中搜索历史对话\n"
                    "- chat_context：给定 message_id 查看前后上下文\n"
                    "- chat_scroll：从某条消息向上/向下翻页浏览更多历史\n\n"
                    "只有在需要查询互联网公开信息（新闻、资料、文档等）时，才使用 web_search。"
                ),
            )

        remote_name = self._resolve_remote_tool_name()
        if not remote_name:
            return ToolResult(
                success=False,
                error="TAVILY_SEARCH_NOT_AVAILABLE",
                message="未找到 Tavily search 工具，请检查 MCP 配置与连接状态",
            )

        payload = {"query": query}
        if max_results is not None:
            payload["max_results"] = max_results
        if search_depth:
            payload["search_depth"] = search_depth
        if include_domains:
            payload["include_domains"] = include_domains

        result = await self._registry.execute(remote_name, **payload)
        if result.success:
            return ToolResult(
                success=True,
                data={"query": query, "search_result": result.data},
                message=f"搜索成功：{query}",
                metadata={"source_tool": remote_name},
            )

        return ToolResult(
            success=False,
            error=result.error,
            message=result.message or "网页搜索失败",
            metadata={"source_tool": remote_name},
        )
