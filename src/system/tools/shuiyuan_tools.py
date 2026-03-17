"""
水源社区工具集。

提供工具供 Agent 访问上海交通大学水源社区（Discourse 论坛）：
- shuiyuan_search：搜索水源社区
- shuiyuan_get_topic：获取单个话题详情
- shuiyuan_post_reply：在水源社区话题中发帖/回复
"""

from __future__ import annotations

from typing import Any, Optional

from agent_core.config import Config, ShuiyuanConfig

from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult


def _get_shuiyuan_client(config: Optional[Config]) -> Optional[Any]:
    """
    获取水源社区客户端实例（支持单 Key 或多 Key 池）。

    若传入的 config 已启用水源但未解析出 user_api_keys（例如 daemon 与 config 加载时机不一致），
    会回退到全局 get_config() 再试一次，确保 config.yaml 里配了 user_api_keys 时工具可用。

    Returns:
        ShuiyuanClient / ShuiyuanClientPool 实例，或 None（未配置或未启用）
    """
    try:
        from frontend.shuiyuan_integration.reply import get_shuiyuan_client_from_config
    except ImportError:
        return None

    def _try(cfg_obj: Optional[Config]) -> Optional[Any]:
        if cfg_obj is None:
            return None
        if not cfg_obj.shuiyuan.enabled:
            return None
        return get_shuiyuan_client_from_config(cfg_obj)

    client = _try(config)
    if client is not None:
        return client
    # 已启用水源但未拿到 client（常见原因：传入的 config 中 user_api_keys 未正确解析）
    if config is not None and config.shuiyuan.enabled:
        try:
            from agent_core.config import get_config
            client = _try(get_config())
            if client is not None:
                return client
        except Exception:
            pass
    return None


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
            "posts": [
                {
                    "id": p.get("id"),
                    "post_number": p.get("post_number"),
                    "username": p.get("username"),
                    "raw": (p.get("raw") or p.get("cooked") or "")[:500],
                }
                for p in posts
            ],
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
                {
                    "description": "给帖子点赞",
                    "params": {"post_id": 123456, "emoji": "thumbsup"},
                },
                {
                    "description": "贴个心",
                    "params": {"post_id": 123456, "emoji": "heart"},
                },
                {
                    "description": "贴笑哭",
                    "params": {"post_id": 123456, "emoji": "joy"},
                },
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


class ShuiyuanGetLatestTool(BaseTool):
    """获取水源社区最新话题列表（首页）。"""

    def __init__(self, config: Optional[Config] = None, max_results: int = 30):
        self._config = config
        self._max_results = max(10, min(50, max_results))

    @property
    def name(self) -> str:
        return "shuiyuan_get_latest"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shuiyuan_get_latest",
            description="""获取水源社区（上海交通大学 Discourse 论坛）的最新话题列表，相当于论坛首页。

适用场景（必须调用此工具获取真实数据，禁止编造）：
- 用户说"看看首页""查看最新帖子""浏览水源首页"
- 用户问"首页有什么""最近有什么帖子"
- 用户要求你从首页挑选/推荐/总结帖子

**重要**：当用户涉及"首页"、"最新帖子"等需求时，必须先调用此工具获取真实数据，然后基于返回的真实话题列表进行回复。禁止在未调用工具的情况下声称"我看了首页"。

返回话题列表，包含标题、作者、回复数、浏览数、最后活动时间等。""",
            parameters=[
                ToolParameter(
                    name="page",
                    type="integer",
                    description="页码，从0开始，默认0",
                    required=False,
                ),
                ToolParameter(
                    name="order",
                    type="string",
                    description="排序方式: default(默认), created(按创建时间), activity(按活跃度)",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "查看首页最新帖子",
                    "params": {},
                },
                {
                    "description": "查看第二页",
                    "params": {"page": 1},
                },
                {
                    "description": "按活跃度排序",
                    "params": {"order": "activity"},
                },
            ],
            usage_notes=[
                "需在 config.yaml 中配置 shuiyuan.enabled=true 和 user_api_key",
                "返回结果默认最多30条，避免上下文过长",
            ],
            tags=["水源", "水源社区", "最新", "首页", "浏览", "Discourse"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        client = _get_shuiyuan_client(self._config)
        if client is None:
            return _shuiyuan_disabled_result(self._config)

        page = int(kwargs.get("page", 0))
        order = str(kwargs.get("order", "default")).strip()
        if order not in ("default", "created", "activity"):
            order = "default"

        try:
            result = client.get_latest_topics(
                page=page,
                per_page=self._max_results,
                order=order,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error="SHUIYUAN_GET_LATEST_FAILED",
                message=f"获取最新话题失败: {e}",
            )

        topics = result.get("topic_list", {}).get("topics", [])
        users = result.get("users", [])
        user_map = {u.get("id"): u.get("username") for u in users}

        simplified = [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "fancy_title": t.get("fancy_title"),
                "posts_count": t.get("posts_count"),
                "views": t.get("views"),
                "created_at": t.get("created_at"),
                "last_posted_at": t.get("last_posted_at"),
                "author": user_map.get(t.get("poster_user_id")),
                "pinned": t.get("pinned", False),
                "category_id": t.get("category_id"),
            }
            for t in topics[: self._max_results]
        ]

        return ToolResult(
            success=True,
            message=f"已获取 {len(simplified)} 条最新话题（第 {page} 页）",
            data={
                "topics": simplified,
                "page": page,
                "total": result.get("topic_list", {}).get("more_topics_url") is not None,
            },
        )


class ShuiyuanGetTopTool(BaseTool):
    """获取水源社区热门话题列表。"""

    def __init__(self, config: Optional[Config] = None, max_results: int = 30):
        self._config = config
        self._max_results = max(10, min(50, max_results))

    @property
    def name(self) -> str:
        return "shuiyuan_get_top"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shuiyuan_get_top",
            description="""获取水源社区（上海交通大学 Discourse 论坛）的热门/置顶话题列表。

适用场景（必须调用此工具获取真实数据，禁止编造）：
- 用户说"看看热门帖子""查看置顶""本周热门"
- 想了解水源社区的热门讨论
- 用户要求你从热门帖中挑选/推荐

**重要**：当用户涉及"热门"、"置顶"等需求时，必须先调用此工具获取真实数据。禁止在未调用工具的情况下声称"我看了热门列表"。

可按时间范围筛选：今日、本周、本月、年度、全部。""",
            parameters=[
                ToolParameter(
                    name="period",
                    type="string",
                    description="时间范围: daily(今日), weekly(本周), monthly(本月), yearly(年度), all(全部)，默认 daily",
                    required=False,
                ),
                ToolParameter(
                    name="page",
                    type="integer",
                    description="页码，从0开始，默认0",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "查看今日热门",
                    "params": {},
                },
                {
                    "description": "查看本周热门",
                    "params": {"period": "weekly"},
                },
                {
                    "description": "查看年度热门",
                    "params": {"period": "yearly"},
                },
            ],
            usage_notes=[
                "需在 config.yaml 中配置 shuiyuan.enabled=true 和 user_api_key",
                "返回结果默认最多30条",
            ],
            tags=["水源", "水源社区", "热门", "置顶", "top", "Discourse"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        client = _get_shuiyuan_client(self._config)
        if client is None:
            return _shuiyuan_disabled_result(self._config)

        period = str(kwargs.get("period", "daily")).strip().lower()
        valid_periods = ("daily", "weekly", "monthly", "yearly", "all")
        if period not in valid_periods:
            period = "daily"

        page = int(kwargs.get("page", 0))

        try:
            result = client.get_top_topics(
                period=period,
                page=page,
                per_page=self._max_results,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error="SHUIYUAN_GET_TOP_FAILED",
                message=f"获取热门话题失败: {e}",
            )

        topics = result.get("topic_list", {}).get("topics", [])
        users = result.get("users", [])
        user_map = {u.get("id"): u.get("username") for u in users}

        simplified = [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "fancy_title": t.get("fancy_title"),
                "posts_count": t.get("posts_count"),
                "views": t.get("views"),
                "like_count": t.get("like_count"),
                "created_at": t.get("created_at"),
                "last_posted_at": t.get("last_posted_at"),
                "author": user_map.get(t.get("poster_user_id")),
                "pinned": t.get("pinned", False),
                "category_id": t.get("category_id"),
            }
            for t in topics[: self._max_results]
        ]

        period_names = {
            "daily": "今日",
            "weekly": "本周",
            "monthly": "本月",
            "yearly": "年度",
            "all": "全部",
        }

        return ToolResult(
            success=True,
            message=f"已获取 {len(simplified)} 条{period_names.get(period, '热门')}话题",
            data={
                "topics": simplified,
                "period": period,
                "page": page,
                "total": result.get("topic_list", {}).get("more_topics_url") is not None,
            },
        )


class ShuiyuanGetCategoriesTool(BaseTool):
    """获取水源社区类别（板块）列表。"""

    def __init__(self, config: Optional[Config] = None):
        self._config = config

    @property
    def name(self) -> str:
        return "shuiyuan_get_categories"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shuiyuan_get_categories",
            description="""获取水源社区（上海交通大学 Discourse 论坛）的所有类别（板块）列表。

适用场景：
- 用户说"有哪些板块""查看类别""浏览分类"
- 用户问水源有哪些分区/板块
- 需要了解水源社区的板块结构

**重要**：当用户询问"有哪些板块/类别"时，必须先调用此工具获取真实数据，然后基于返回的真实类别列表进行回复。禁止在未调用工具的情况下列举板块名称。

返回类别列表，包含类别ID、名称、描述、话题数等。""",
            parameters=[
                ToolParameter(
                    name="include_subcategories",
                    type="boolean",
                    description="是否包含子类别，默认 true",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "获取所有类别",
                    "params": {},
                },
            ],
            usage_notes=[
                "需在 config.yaml 中配置 shuiyuan.enabled=true 和 user_api_key",
                "获取类别ID后，可用 shuiyuan_get_category_topics 查看该类别下的话题",
            ],
            tags=["水源", "水源社区", "类别", "板块", "分类", "Discourse"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        client = _get_shuiyuan_client(self._config)
        if client is None:
            return _shuiyuan_disabled_result(self._config)

        include_sub = kwargs.get("include_subcategories", True)
        if isinstance(include_sub, str):
            include_sub = include_sub.lower() in ("true", "1", "yes")

        try:
            result = client.get_categories(include_subcategories=bool(include_sub))
        except Exception as e:
            return ToolResult(
                success=False,
                error="SHUIYUAN_GET_CATEGORIES_FAILED",
                message=f"获取类别列表失败: {e}",
            )

        categories = result.get("category_list", {}).get("categories", [])

        simplified = []
        for c in categories:
            cat = {
                "id": c.get("id"),
                "name": c.get("name"),
                "description": c.get("description"),
                "topic_count": c.get("topic_count"),
                "post_count": c.get("post_count"),
                "position": c.get("position"),
                "slug": c.get("slug"),
            }
            # 子类别
            subcategories = c.get("subcategory_list", [])
            if subcategories:
                cat["subcategories"] = [
                    {
                        "id": s.get("id"),
                        "name": s.get("name"),
                        "slug": s.get("slug"),
                    }
                    for s in subcategories
                ]
            simplified.append(cat)

        return ToolResult(
            success=True,
            message=f"已获取 {len(simplified)} 个类别",
            data={"categories": simplified},
        )


class ShuiyuanGetCategoryTopicsTool(BaseTool):
    """获取水源社区特定类别下的话题列表。"""

    def __init__(self, config: Optional[Config] = None, max_results: int = 30):
        self._config = config
        self._max_results = max(10, min(50, max_results))

    @property
    def name(self) -> str:
        return "shuiyuan_get_category_topics"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shuiyuan_get_category_topics",
            description="""获取水源社区（上海交通大学 Discourse 论坛）特定类别（板块）下的话题列表。

适用场景：
- 用户说"查看XX板块""看水源开发者分类的帖子"
- 需要浏览特定类别下的话题
- 结合 shuiyuan_get_categories 获取的类别ID使用

参数 category_id 可以是类别ID（数字）或类别slug（如 "shuiyuan-developers"）。""",
            parameters=[
                ToolParameter(
                    name="category_id",
                    type="integer",
                    description="类别ID（从 shuiyuan_get_categories 获取）",
                    required=True,
                ),
                ToolParameter(
                    name="page",
                    type="integer",
                    description="页码，从0开始，默认0",
                    required=False,
                ),
                ToolParameter(
                    name="order",
                    type="string",
                    description="排序方式: default(默认), created(按创建时间), activity(按活跃度)",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "查看类别ID为42的话题",
                    "params": {"category_id": 42},
                },
                {
                    "description": "查看第二页，按活跃度排序",
                    "params": {"category_id": 42, "page": 1, "order": "activity"},
                },
            ],
            usage_notes=[
                "需在 config.yaml 中配置 shuiyuan.enabled=true 和 user_api_key",
                "先用 shuiyuan_get_categories 获取类别ID",
                "返回结果默认最多30条",
            ],
            tags=["水源", "水源社区", "类别", "板块", "话题", "Discourse"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        client = _get_shuiyuan_client(self._config)
        if client is None:
            return _shuiyuan_disabled_result(self._config)

        category_id = kwargs.get("category_id")
        if category_id is None:
            return ToolResult(
                success=False,
                error="MISSING_CATEGORY_ID",
                message="请提供 category_id",
            )

        try:
            category_id = int(category_id)
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                error="INVALID_CATEGORY_ID",
                message="category_id 必须为整数",
            )

        page = int(kwargs.get("page", 0))
        order = str(kwargs.get("order", "default")).strip()
        if order not in ("default", "created", "activity"):
            order = "default"

        try:
            result = client.get_category_topics(
                category_id=category_id,
                page=page,
                per_page=self._max_results,
                order=order,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error="SHUIYUAN_GET_CATEGORY_TOPICS_FAILED",
                message=f"获取类别话题失败: {e}",
            )

        if result.get("error") == "CATEGORY_NOT_FOUND":
            return ToolResult(
                success=False,
                error="CATEGORY_NOT_FOUND",
                message=f"类别 {category_id} 不存在",
            )

        topics = result.get("topic_list", {}).get("topics", [])
        users = result.get("users", [])
        user_map = {u.get("id"): u.get("username") for u in users}

        simplified = [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "fancy_title": t.get("fancy_title"),
                "posts_count": t.get("posts_count"),
                "views": t.get("views"),
                "created_at": t.get("created_at"),
                "last_posted_at": t.get("last_posted_at"),
                "author": user_map.get(t.get("poster_user_id")),
                "pinned": t.get("pinned", False),
            }
            for t in topics[: self._max_results]
        ]

        return ToolResult(
            success=True,
            message=f"已获取 {len(simplified)} 条话题（类别 {category_id}，第 {page} 页）",
            data={
                "topics": simplified,
                "category_id": category_id,
                "page": page,
                "total": result.get("topic_list", {}).get("more_topics_url") is not None,
            },
        )


class ShuiyuanBrowseTopicTool(BaseTool):
    """翻页浏览水源社区话题的帖子（支持从指定楼层开始）。"""

    def __init__(self, config: Optional[Config] = None, posts_limit: int = 50):
        self._config = config
        self._posts_limit = max(10, min(100, posts_limit))

    @property
    def name(self) -> str:
        return "shuiyuan_browse_topic"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="shuiyuan_browse_topic",
            description="""翻页浏览水源社区话题的帖子，支持从指定楼层开始查看。

适用场景：
- 用户说"翻看本楼帖子""从第N楼开始看"
- 需要按顺序浏览话题的帖子（区别于获取最近N条）
- 长帖翻页阅读

**重要**：当用户要求"翻看"、"浏览"特定话题时，必须先调用此工具获取真实帖子数据，然后基于返回的真实内容进行回复。禁止在未调用工具的情况下声称"我已经翻看了帖子"。

与 shuiyuan_get_topic 不同：
- shuiyuan_get_topic：获取话题元信息+最近N条帖子
- shuiyuan_browse_topic：按楼层顺序获取帖子，支持从任意位置开始

参数 topic_id 可从 shuiyuan_get_latest、shuiyuan_search 等工具的返回中获取。""",
            parameters=[
                ToolParameter(
                    name="topic_id",
                    type="integer",
                    description="话题 ID，可从水源社区 URL /t/topic/{topic_id} 中获取",
                    required=True,
                ),
                ToolParameter(
                    name="start_post_number",
                    type="integer",
                    description="起始楼层号（从1开始），默认1",
                    required=False,
                ),
                ToolParameter(
                    name="limit",
                    type="integer",
                    description="获取帖子数量，默认50，最大100",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "从第1楼开始浏览话题",
                    "params": {"topic_id": 456220},
                },
                {
                    "description": "从第51楼开始浏览",
                    "params": {"topic_id": 456220, "start_post_number": 51},
                },
                {
                    "description": "只看前20条",
                    "params": {"topic_id": 456220, "limit": 20},
                },
            ],
            usage_notes=[
                "需在 config.yaml 中配置 shuiyuan.enabled=true 和 user_api_key",
                "适合长帖翻页阅读场景",
                "返回结果包含帖子楼层号，便于用户定位",
            ],
            tags=["水源", "水源社区", "浏览", "翻页", "帖子", "Discourse"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        client = _get_shuiyuan_client(self._config)
        if client is None:
            return _shuiyuan_disabled_result(self._config)

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

        start_post = int(kwargs.get("start_post_number", 1))
        limit = int(kwargs.get("limit", self._posts_limit))
        limit = max(10, min(100, limit))

        try:
            posts = client.get_topic_posts_paged(
                topic_id=topic_id,
                post_number_start=start_post,
                limit=limit,
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error="SHUIYUAN_BROWSE_TOPIC_FAILED",
                message=f"浏览话题失败: {e}",
            )

        if not posts:
            return ToolResult(
                success=False,
                error="TOPIC_EMPTY_OR_NOT_FOUND",
                message=f"话题 {topic_id} 不存在或从 {start_post} 楼开始无帖子",
            )

        simplified = [
            {
                "id": p.get("id"),
                "post_number": p.get("post_number"),
                "username": p.get("username"),
                "raw": (p.get("raw") or p.get("cooked", ""))[:800],
                "created_at": p.get("created_at"),
                "like_count": p.get("like_count", 0),
            }
            for p in posts
        ]

        end_post = start_post + len(posts) - 1
        return ToolResult(
            success=True,
            message=f"已获取话题 {topic_id} 第 {start_post}-{end_post} 楼的 {len(simplified)} 条帖子",
            data={
                "topic_id": topic_id,
                "posts": simplified,
                "start_post_number": start_post,
                "end_post_number": end_post,
                "has_more": len(posts) >= limit,
            },
        )


def _shuiyuan_disabled_result(config: Optional[Config]) -> ToolResult:
    """统一返回水源未启用的错误结果。"""
    if config and config.shuiyuan.enabled:
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
