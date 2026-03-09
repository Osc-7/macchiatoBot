"""
水源社区工具集。

提供工具供 Agent 访问上海交通大学水源社区（Discourse 论坛）：
- shuiyuan_search：搜索水源社区
- shuiyuan_get_topic：获取单个话题详情
- shuiyuan_post_reply：在水源社区话题中发帖/回复
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

from agent_core.config import Config, ShuiyuanConfig

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult


def _get_shuiyuan_client(config: Optional[Config]) -> Optional[Any]:
    """
    获取水源社区客户端实例（支持单 Key 或多 Key 池）。

    Returns:
        ShuiyuanClient / ShuiyuanClientPool 实例，或 None（未配置或未启用）
    """
    if config is None:
        return None

    cfg: ShuiyuanConfig = config.shuiyuan
    if not cfg.enabled:
        return None

    # 复用 shuiyuan_integration.reply 中的构造逻辑（支持 user_api_keys + 日级限流切换）
    try:
        from frontend.shuiyuan_integration.reply import get_shuiyuan_client_from_config
    except ImportError:
        return None

    return get_shuiyuan_client_from_config(config)


class ShuiyuanSearchTool(BaseTool):
    """搜索水源社区。"""

    def __init__(self, config: Optional[Config] = None, max_results: int = 50):
        self._config = config
        self._max_results = max(10, min(100, max_results))

    @property
    def name(self) -> str:
        return "shuiyuan_search"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shuiyuan_search",
            description="""在水源社区（上海交通大学 Discourse 论坛）内搜索话题和帖子。返回结果截断为最近 N 条（默认 50），避免上下文过长。

适用场景：
- 用户想在水源社区搜索某类话题、标签、关键词
- 需要了解水源社区内某主题的讨论情况
- 查找水源开发者、灌水楼等特定板块的帖子
- 搜索某用户的历史发言：使用 user:用户名

搜索支持 Discourse 语法，如：
- 关键词：水源 课表
- 标签：tags:水源开发者、tags:灌水
- 用户历史：user:Osc7、user:用户名 关键词

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
                    "description": "搜索某用户历史发言",
                    "params": {"q": "user:Osc7 玛奇朵"},
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
        client = _get_shuiyuan_client(self._config)
        if client is None:
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

        q = kwargs.get("q", "").strip()
        if not q:
            return ToolResult(
                success=False,
                error="MISSING_QUERY",
                message="请提供搜索关键词 q",
            )

        page = int(kwargs.get("page", 1))

        try:
            result = client.search(q=q, page=page)
        except Exception as e:
            return ToolResult(
                success=False,
                error="SHUIYUAN_SEARCH_FAILED",
                message=f"水源社区搜索失败: {e}",
            )

        # 截断：最多返回 max_results 条 posts
        max_n = self._max_results
        if isinstance(result, dict):
            posts = result.get("posts") or []
            if isinstance(posts, list) and len(posts) > max_n:
                result = dict(result)
                result["posts"] = posts[:max_n]
                result["_truncated"] = True
                result["_total_posts"] = len(posts)
            grp = result.get("grouped_search_result") or {}
            if isinstance(grp, dict) and grp.get("post_ids"):
                pids = grp["post_ids"]
                if len(pids) > max_n:
                    result = dict(result)
                    if "grouped_search_result" not in result:
                        result["grouped_search_result"] = dict(grp)
                    result["grouped_search_result"]["post_ids"] = pids[:max_n]
                    result["_truncated"] = True

        msg = "水源社区搜索完成"
        return ToolResult(success=True, message=msg, data=result)


class ShuiyuanGetTopicTool(BaseTool):
    """获取水源社区单个话题详情。"""

    def __init__(self, config: Optional[Config] = None, posts_limit: int = 50):
        self._config = config
        self._posts_limit = max(10, min(100, posts_limit))

    @property
    def name(self) -> str:
        return "shuiyuan_get_topic"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shuiyuan_get_topic",
            description="""获取水源社区（上海交通大学 Discourse 论坛）中单个话题的详情。仅返回最近 N 条帖子（默认 50），避免上下文过长。

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
        client = _get_shuiyuan_client(self._config)
        if client is None:
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
            topic = client.get_topic(topic_id)
        except Exception as e:
            return ToolResult(
                success=False,
                error="SHUIYUAN_GET_TOPIC_FAILED",
                message=f"获取水源社区话题失败: {e}",
            )

        if topic is None:
            return ToolResult(
                success=False,
                error="TOPIC_NOT_FOUND",
                message=f"未找到话题 {topic_id}，可能已删除或不存在",
            )

        # 截断：仅返回最近 N 条帖子
        posts = client.get_topic_recent_posts(topic_id, limit=self._posts_limit)
        result: dict[str, Any] = {
            "id": topic.get("id"),
            "title": topic.get("title"),
            "fancy_title": topic.get("fancy_title"),
            "posts_count": topic.get("posts_count"),
            "posts": [{"id": p.get("id"), "post_number": p.get("post_number"), "username": p.get("username"), "raw": (p.get("raw") or p.get("cooked") or "")[:500]} for p in posts],
            "_posts_limit": self._posts_limit,
            "_truncated": (topic.get("posts_count") or 0) > self._posts_limit,
        }
        return ToolResult(
            success=True,
            message=f"已获取话题「{topic.get('title', '')}」（最近 {len(posts)} 条）",
            data=result,
        )


class ShuiyuanRetortTool(BaseTool):
    """对水源帖子贴表情（Retort 插件）。toggle：已贴则取消，未贴则添加。"""

    def __init__(self, config: Optional[Config] = None):
        self._config = config

    @property
    def name(self) -> str:
        return "shuiyuan_post_retort"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shuiyuan_post_retort",
            description="""对水源社区帖子贴表情。

使用 Retort 插件，与水源助手（Greasy Fork）一致。toggle 行为：已贴则取消，未贴则添加。

适用场景：
- 用户说「给这个帖点个赞」「贴个心」「加个笑哭」等
- 对某条帖子表示认同、感谢、有趣等

emoji 使用标准名，如：thumbsup、+1、heart、smile、joy、fire、100 等，不要带冒号。
post_id 可从 shuiyuan_get_topic 返回的 posts 中的 id 字段获取。""",
            parameters=[
                ToolParameter(
                    name="post_id",
                    type="integer",
                    description="帖子 ID，可从 shuiyuan_get_topic 的 posts[].id 获取",
                    required=True,
                ),
                ToolParameter(
                    name="emoji",
                    type="string",
                    description="表情名，如 thumbsup、heart、smile、joy、fire、100，不要带冒号",
                    required=True,
                ),
                ToolParameter(
                    name="topic_id",
                    type="integer",
                    description="可选，话题 ID，用于部分场景下的校验",
                    required=False,
                ),
            ],
            examples=[
                {"description": "给帖子点赞", "params": {"post_id": 123456, "emoji": "thumbsup"}},
                {"description": "贴个心", "params": {"post_id": 123456, "emoji": "heart"}},
                {"description": "贴笑哭", "params": {"post_id": 123456, "emoji": "joy"}},
            ],
            usage_notes=[
                "需配置 shuiyuan.enabled=true 和 user_api_key（或 SHUIYUAN_USER_API_KEY）",
                "支持水源自定义表情，如 sjtu、shuiyuan 等",
            ],
            tags=["水源", "水源社区", "贴表情", "Retort", "Discourse"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        client = _get_shuiyuan_client(self._config)
        if client is None:
            if self._config and self._config.shuiyuan.enabled:
                return ToolResult(
                    success=False,
                    error="SHUIYUAN_API_KEY_MISSING",
                    message="水源社区已启用但未配置 User-Api-Key，请设置 shuiyuan.user_api_key 或 SHUIYUAN_USER_API_KEY",
                )
            return ToolResult(
                success=False,
                error="SHUIYUAN_DISABLED",
                message="水源社区工具未启用，请在 config.yaml 中设置 shuiyuan.enabled=true",
            )

        post_id = kwargs.get("post_id")
        emoji_raw = (kwargs.get("emoji") or "").strip()
        topic_id = kwargs.get("topic_id")

        if post_id is None:
            return ToolResult(
                success=False,
                error="MISSING_POST_ID",
                message="请提供 post_id",
            )
        try:
            post_id = int(post_id)
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                error="INVALID_POST_ID",
                message="post_id 必须为整数",
            )

        if not emoji_raw:
            return ToolResult(
                success=False,
                error="MISSING_EMOJI",
                message="请提供 emoji，如 thumbsup、heart",
            )

        topic_id_int: Optional[int] = None
        if topic_id is not None:
            try:
                topic_id_int = int(topic_id)
            except (TypeError, ValueError):
                pass

        try:
            ok, status, detail = client.toggle_retort(post_id, emoji_raw, topic_id_int)
        except Exception as e:
            return ToolResult(
                success=False,
                error="SHUIYUAN_RETORT_FAILED",
                message=f"贴表情失败: {e}",
            )

        if ok:
            return ToolResult(
                success=True,
                message=f"已对帖子 {post_id} toggle 表情 :{emoji_raw}:",
                data={"post_id": post_id, "emoji": emoji_raw},
            )
        return ToolResult(
            success=False,
            error="SHUIYUAN_RETORT_ERROR",
            message=f"贴表情失败 (HTTP {status}): {detail or '未知错误'}",
        )


class ShuiyuanPostReplyTool(BaseTool):
    """在水源社区话题中发帖/回复。需 User-Api-Key 含 write scope。"""

    def __init__(
        self,
        config: Optional[Config] = None,
        username: str = "",
        topic_id: int = 0,
        reply_to_post_number: Optional[int] = None,
    ):
        self._config = config
        self._username = username
        self._topic_id = topic_id
        self._reply_to_post_number = reply_to_post_number

    @property
    def name(self) -> str:
        return "shuiyuan_post_reply"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shuiyuan_post_reply",
            description="""在水源社区当前话题中发帖或回复。

使用场景：被 @ 后需要在该楼回复时调用。限流：每用户每分钟有回复次数上限。

参数 raw 为要发送的正文（支持 Markdown）。topic_id 和 reply_to_post_number 由会话上下文提供，无需传入。""",
            parameters=[
                ToolParameter(
                    name="raw",
                    type="string",
                    description="要发送的正文内容（支持 Markdown）",
                    required=True,
                ),
            ],
            usage_notes=[
                "发帖需 User-Api-Key 含 write scope。运行 python -m shuiyuan_integration.user_api_key 可生成（默认已含 read+write）。若仅 read，发帖会失败，需重新生成 Key。",
                "限流时返回错误，请告知用户稍后再试",
            ],
            tags=["水源", "水源社区", "发帖", "Discourse"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        client = _get_shuiyuan_client(self._config)
        if client is None:
            return ToolResult(
                success=False,
                error="SHUIYUAN_DISABLED",
                message="水源社区未配置或未启用",
            )
        if not self._username or not self._topic_id:
            return ToolResult(
                success=False,
                error="MISSING_SESSION_CONTEXT",
                message="shuiyuan_post_reply 需在水源会话上下文中调用，当前缺少 username 或 topic_id",
            )

        raw = kwargs.get("raw", "").strip()
        if not raw:
            return ToolResult(
                success=False,
                error="MISSING_RAW",
                message="请提供要发送的正文 raw",
            )

        try:
            from frontend.shuiyuan_integration.db import ShuiyuanDB
            from frontend.shuiyuan_integration.reply import post_reply
        except ImportError as e:
            return ToolResult(
                success=False,
                error="SHUIYUAN_IMPORT_ERROR",
                message=f"无法导入水源集成模块: {e}",
            )

        # 理论上构造该 Tool 时一定会传入 Config，这里做一次防御性检查
        if not self._config:
            return ToolResult(
                success=False,
                error="SHUIYUAN_DISABLED",
                message="水源社区未配置或未启用",
            )

        cfg = self._config.shuiyuan
        from frontend.shuiyuan_integration.db import get_shuiyuan_db_path_for_user

        base_dir = getattr(cfg, "db_base_dir", None) or "./data/shuiyuan"
        db_path = get_shuiyuan_db_path_for_user(base_dir, self._username)
        db = ShuiyuanDB(
            db_path=db_path,
            chat_limit_per_user=cfg.memory.chat_limit_per_user,
            replies_per_minute=cfg.rate_limit.replies_per_minute,
        )
        success, msg = post_reply(
            username=self._username,
            topic_id=self._topic_id,
            raw=raw,
            reply_to_post_number=self._reply_to_post_number,
            db=db,
            client=client,
        )
        if success:
            return ToolResult(success=True, message=msg, data={"posted": True})
        return ToolResult(success=False, error="SHUIYUAN_POST_FAILED", message=msg)


