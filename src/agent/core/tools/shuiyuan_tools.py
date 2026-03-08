"""
水源社区工具集。

提供只读工具，供 Agent 访问上海交通大学水源社区（Discourse 论坛）：
- shuiyuan_search：搜索水源社区
- shuiyuan_get_topic：获取单个话题详情
"""

from __future__ import annotations

import os
from typing import Optional

from agent.config import Config, ShuiyuanConfig

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult


def _get_shuiyuan_client(config: Optional[Config]) -> Optional[tuple[str, str]]:
    """
    获取水源社区 User-Api-Key 和 site_url。

    Returns:
        (user_api_key, site_url) 或 None（未配置或未启用）
    """
    cfg: ShuiyuanConfig = config.shuiyuan if config else ShuiyuanConfig()
    if not cfg.enabled:
        return None

    key = cfg.user_api_key or os.environ.get("SHUIYUAN_USER_API_KEY")
    if not key:
        return None

    return (key.strip(), cfg.site_url or "https://shuiyuan.sjtu.edu.cn")


class ShuiyuanSearchTool(BaseTool):
    """搜索水源社区。"""

    def __init__(self, config: Optional[Config] = None):
        self._config = config

    @property
    def name(self) -> str:
        return "shuiyuan_search"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shuiyuan_search",
            description="""在水源社区（上海交通大学 Discourse 论坛）内搜索话题和帖子。

适用场景：
- 用户想在水源社区搜索某类话题、标签、关键词
- 需要了解水源社区内某主题的讨论情况
- 查找水源开发者、灌水楼等特定板块的帖子

搜索支持 Discourse 语法，如：
- 关键词：水源 课表
- 标签：tags:水源开发者、tags:灌水
- 用户：@username

工具会返回话题列表，包含标题、链接、摘要等。""",
            parameters=[
                ToolParameter(
                    name="q",
                    type="string",
                    description="搜索关键词或 Discourse 语法，如 'tags:水源开发者' 或 '灌水'",
                    required=True,
                ),
                ToolParameter(
                    name="page",
                    type="integer",
                    description="页码，默认 1",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "搜索水源开发者板块",
                    "params": {"q": "tags:水源开发者"},
                },
                {
                    "description": "搜索灌水相关帖子",
                    "params": {"q": "灌水"},
                },
            ],
            usage_notes=[
                "需在 config.yaml 中配置 shuiyuan.enabled=true 和 user_api_key（或环境变量 SHUIYUAN_USER_API_KEY）",
                "获取 User-Api-Key：运行 python -m shuiyuan_integration.user_api_key",
            ],
            tags=["水源", "水源社区", "搜索", "Discourse"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        client_info = _get_shuiyuan_client(self._config)
        if client_info is None:
            if self._config and self._config.shuiyuan.enabled:
                return ToolResult(
                    success=False,
                    error="SHUIYUAN_API_KEY_MISSING",
                    message="水源社区已启用但未配置 User-Api-Key，请设置 shuiyuan.user_api_key 或环境变量 SHUIYUAN_USER_API_KEY。获取方式：运行 python -m shuiyuan_integration.user_api_key",
                )
            return ToolResult(
                success=False,
                error="SHUIYUAN_DISABLED",
                message="水源社区工具未启用，请在 config.yaml 中设置 shuiyuan.enabled=true",
            )

        try:
            from shuiyuan_integration import ShuiyuanClient
        except ImportError as e:
            return ToolResult(
                success=False,
                error="SHUIYUAN_IMPORT_ERROR",
                message=f"无法导入水源集成模块: {e}",
            )

        key, site_url = client_info
        q = kwargs.get("q", "").strip()
        if not q:
            return ToolResult(
                success=False,
                error="MISSING_QUERY",
                message="请提供搜索关键词 q",
            )

        page = int(kwargs.get("page", 1))

        try:
            client = ShuiyuanClient(user_api_key=key, site_url=site_url)
            result = client.search(q=q, page=page)
        except Exception as e:
            return ToolResult(
                success=False,
                error="SHUIYUAN_SEARCH_FAILED",
                message=f"水源社区搜索失败: {e}",
            )

        msg = "水源社区搜索完成"
        return ToolResult(success=True, message=msg, data=result)


class ShuiyuanGetTopicTool(BaseTool):
    """获取水源社区单个话题详情。"""

    def __init__(self, config: Optional[Config] = None):
        self._config = config

    @property
    def name(self) -> str:
        return "shuiyuan_get_topic"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shuiyuan_get_topic",
            description="""获取水源社区（上海交通大学 Discourse 论坛）中单个话题的详情。

适用场景：
- 已知话题 ID，需要获取标题、正文、回复等完整内容
- 查看某个帖子的具体讨论

参数 topic_id 可从 shuiyuan_search 的返回中获取，或从水源社区 URL 中提取（如 /t/topic/123456 中的 123456）。""",
            parameters=[
                ToolParameter(
                    name="topic_id",
                    type="integer",
                    description="话题 ID，可从水源社区 URL /t/topic/{topic_id} 中获取",
                    required=True,
                ),
            ],
            examples=[
                {
                    "description": "获取话题 456220 的详情",
                    "params": {"topic_id": 456220},
                },
            ],
            usage_notes=[
                "需配置 shuiyuan.enabled=true 和 user_api_key（或 SHUIYUAN_USER_API_KEY）",
            ],
            tags=["水源", "水源社区", "话题", "Discourse"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        client_info = _get_shuiyuan_client(self._config)
        if client_info is None:
            if self._config and self._config.shuiyuan.enabled:
                return ToolResult(
                    success=False,
                    error="SHUIYUAN_API_KEY_MISSING",
                    message="水源社区已启用但未配置 User-Api-Key，请设置 shuiyuan.user_api_key 或环境变量 SHUIYUAN_USER_API_KEY",
                )
            return ToolResult(
                success=False,
                error="SHUIYUAN_DISABLED",
                message="水源社区工具未启用，请在 config.yaml 中设置 shuiyuan.enabled=true",
            )

        try:
            from shuiyuan_integration import ShuiyuanClient
        except ImportError as e:
            return ToolResult(
                success=False,
                error="SHUIYUAN_IMPORT_ERROR",
                message=f"无法导入水源集成模块: {e}",
            )

        key, site_url = client_info
        topic_id = kwargs.get("topic_id")
        if topic_id is None:
            return ToolResult(
                success=False,
                error="MISSING_TOPIC_ID",
                message="请提供话题 ID topic_id",
            )

        try:
            topic_id = int(topic_id)
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                error="INVALID_TOPIC_ID",
                message="topic_id 必须为整数",
            )

        try:
            client = ShuiyuanClient(user_api_key=key, site_url=site_url)
            result = client.get_topic(topic_id)
        except Exception as e:
            return ToolResult(
                success=False,
                error="SHUIYUAN_GET_TOPIC_FAILED",
                message=f"获取水源社区话题失败: {e}",
            )

        if result is None:
            return ToolResult(
                success=False,
                error="TOPIC_NOT_FOUND",
                message=f"未找到话题 {topic_id}，可能已删除或不存在",
            )

        return ToolResult(
            success=True,
            message=f"已获取话题「{result.get('title', '')}」",
            data=result,
        )
