"""
网页抓取工具

通过 LLM 的网页抓取功能访问指定 URL 并提取内容。
"""

from typing import Optional

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from schedule_agent.config import Config, LLMConfig, SearchOptionsConfig, get_config
from schedule_agent.core.llm import LLMClient


class WebExtractorTool(BaseTool):
    """
    网页抓取工具

    访问指定 URL 并提取、总结网页内容。
    工具内部使用独立的 LLM 调用（启用网页抓取，但不使用其他工具）。
    """

    def __init__(self, config: Optional[Config] = None):
        """
        初始化网页抓取工具。

        Args:
            config: 配置对象，如果为 None 则使用全局配置
        """
        self._config = config or get_config()
        # 创建专用的 LLM 客户端（启用网页抓取，但不传其他工具）
        self._llm_client = self._create_web_extractor_client()

    def _create_web_extractor_client(self) -> LLMClient:
        """
        创建用于网页抓取的 LLM 客户端。

        配置：
        - enable_search: true
        - enable_web_extractor: true
        - enable_thinking: true（自动设置）
        - search_strategy: agent_max（自动设置）

        Returns:
            LLMClient 实例
        """
        # 创建网页抓取专用配置
        # 注意：enable_web_extractor 不在全局使用，工具内部会直接调用流式 API
        web_extractor_config = Config(
            llm=LLMConfig(
                provider=self._config.llm.provider,
                api_key=self._config.llm.api_key,
                base_url=self._config.llm.base_url,
                model=self._config.llm.model,
                temperature=self._config.llm.temperature,
                max_tokens=self._config.llm.max_tokens,
                enable_search=True,
                enable_thinking=True,
                search_options=SearchOptionsConfig(
                    search_strategy="agent_max",
                ),
            ),
            time=self._config.time,
            storage=self._config.storage,
            agent=self._config.agent,
            logging=self._config.logging,
        )
        return LLMClient(config=web_extractor_config)

    @property
    def name(self) -> str:
        return "extract_web_content"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="extract_web_content",
            description="""访问指定网页 URL 并提取、总结其内容。

当用户想要：
- 查看某个网页的内容
- 总结网页或文档
- 提取网页中的关键信息
- 分析网页内容

工具会自动：
- 访问指定的 URL
- 提取网页内容
- 总结关键信息
- 返回结构化的结果

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
                "工具会自动提取网页的主要内容并总结",
                "如果指定了 query，工具会根据查询要求提取相关信息",
                "查询火车/航班时刻时，务必在 query 中写明出发地、目的地（如「厦门到上海 G260 时刻表」），否则可能返回同名车次/航班的其它线路信息。",
            ],
            tags=['网络', '抓取'],
        )

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

        try:
            # 构建用户消息
            if query:
                user_message = f"请访问 {url} 并{query}。"
            else:
                user_message = f"请访问 {url} 并总结其内容。"

            # 使用专用的 LLM 客户端（启用网页抓取，但不传其他工具）
            # 网页抓取必须使用流式模式，直接调用流式方法
            full_messages = [
                {"role": "system", "content": "你是一个网页内容分析助手。请访问用户提供的 URL，提取并总结网页内容。"},
                {"role": "user", "content": user_message},
            ]
            
            request_params = {
                "model": self._llm_client.model,
                "messages": full_messages,
                "temperature": self._llm_client.temperature,
                "max_tokens": self._llm_client.max_tokens,
                "extra_body": {
                    "enable_search": True,
                    "enable_thinking": True,
                    "search_options": {
                        "search_strategy": "agent_max",
                    },
                },
            }
            
            response = await self._llm_client._chat_with_tools_stream(request_params)

            if response.content:
                return ToolResult(
                    success=True,
                    data={"url": url, "content": response.content},
                    message=f"成功提取网页内容：{url}",
                    metadata={"usage": response.usage.__dict__ if response.usage else None},
                )
            else:
                return ToolResult(
                    success=False,
                    error="NO_CONTENT",
                    message="未能获取网页内容，请检查 URL 是否可访问",
                )

        except Exception as e:
            return ToolResult(
                success=False,
                error="EXTRACTION_ERROR",
                message=f"网页抓取失败: {str(e)}",
            )

    async def close(self) -> None:
        """关闭工具，释放资源"""
        await self._llm_client.close()
