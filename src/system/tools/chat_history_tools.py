"""
对话历史检索工具

提供三个 LLM 可调用的工具，用于搜索和浏览历史对话记录：
- chat_search: FTS5 关键词搜索
- chat_context: 获取指定消息的前后上下文
- chat_scroll: 从锚点消息继续翻页
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult

if TYPE_CHECKING:
    from agent_core.memory.chat_history_db import ChatHistoryDB


class ChatSearchTool(BaseTool):
    """FTS5 全文搜索历史对话。"""

    def __init__(self, db: "ChatHistoryDB") -> None:
        self._db = db

    @property
    def name(self) -> str:
        return "chat_search"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="chat_search",
            description=(
                "在历史对话记录中搜索相关内容。使用全文索引进行关键词匹配，"
                "返回最相关的消息片段及其 ID。\n\n"
                "适用场景：\n"
                "- 查找之前讨论过某个话题的记录\n"
                "- 回溯用户提过的偏好或决定\n"
                "- 找到某次提到的文件、时间或事件\n\n"
                "查询语义说明：\n"
                "- 单个词：匹配包含该词的历史消息\n"
                "- 多个词（空格分隔）：匹配「包含任意一个关键词」的消息（OR 语义），"
                "适合用 2~4 个核心关键词来缩小范围。"
            ),
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description="搜索关键词，支持多个词（空格分隔）",
                    required=True,
                ),
                ToolParameter(
                    name="top_k",
                    type="integer",
                    description="返回结果数量上限，默认 5",
                    required=False,
                    default=5,
                ),
            ],
            examples=[
                {
                    "description": "搜索关于项目报告的历史对话",
                    "params": {"query": "项目报告", "top_k": 5},
                },
                {
                    "description": "查找提到周五截止日期的记录",
                    "params": {"query": "周五 截止", "top_k": 3},
                },
            ],
            usage_notes=[
                "搜索结果按相关度排序，snippet 字段显示匹配片段（关键词用 [] 标记）",
                "获取到 message_id 后可用 chat_context 查看完整上下文",
                "tool 消息内容可能被截断到 500 字",
            ],
            tags=["memory", "search", "history"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        query: str = kwargs.get("query", "")
        top_k: int = int(kwargs.get("top_k", 5))

        if not query.strip():
            return ToolResult(
                success=False,
                error="MISSING_QUERY",
                message="搜索关键词不能为空",
            )

        try:
            results = self._db.search(query, top_k=top_k)
        except Exception as e:
            return ToolResult(
                success=False,
                error="SEARCH_ERROR",
                message=f"搜索失败: {e}",
            )

        if not results:
            return ToolResult(
                success=True,
                data=[],
                message=f'未找到与 "{query}" 相关的历史对话',
            )

        return ToolResult(
            success=True,
            data=results,
            message=f"找到 {len(results)} 条相关记录",
        )


class ChatContextTool(BaseTool):
    """获取指定消息的前后上下文。"""

    def __init__(self, db: "ChatHistoryDB") -> None:
        self._db = db

    @property
    def name(self) -> str:
        return "chat_context"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="chat_context",
            description=(
                "获取指定消息 ID 前后各 n 条消息，以还原完整对话上下文。\n\n"
                "适用场景：\n"
                "- 通过 chat_search 找到相关消息后，查看其完整上下文\n"
                "- 了解某条决策前后发生了什么\n"
                "- 查看某次工具调用的完整过程"
            ),
            parameters=[
                ToolParameter(
                    name="message_id",
                    type="integer",
                    description="消息 ID（来自 chat_search 的结果）",
                    required=True,
                ),
                ToolParameter(
                    name="n",
                    type="integer",
                    description="前后各取几条消息，默认 5",
                    required=False,
                    default=5,
                ),
            ],
            examples=[
                {
                    "description": "查看消息 ID 42 前后各 3 条的上下文",
                    "params": {"message_id": 42, "n": 3},
                },
            ],
            usage_notes=[
                "返回的消息按时间顺序排列（最旧到最新）",
                "若需要继续向上或向下翻页，使用 chat_scroll",
            ],
            tags=["memory", "history", "context"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        message_id = kwargs.get("message_id")
        n: int = int(kwargs.get("n", 5))

        if message_id is None:
            return ToolResult(
                success=False,
                error="MISSING_MESSAGE_ID",
                message="必须提供 message_id",
            )

        try:
            message_id = int(message_id)
            results = self._db.get_context(message_id, n=n)
        except Exception as e:
            return ToolResult(
                success=False,
                error="CONTEXT_ERROR",
                message=f"获取上下文失败: {e}",
            )

        if not results:
            return ToolResult(
                success=True,
                data=[],
                message=f"未找到消息 ID {message_id}",
            )

        return ToolResult(
            success=True,
            data=results,
            message=f"共返回 {len(results)} 条消息（以 ID {message_id} 为中心）",
        )


class ChatScrollTool(BaseTool):
    """从锚点消息向上/向下翻页。"""

    def __init__(self, db: "ChatHistoryDB") -> None:
        self._db = db

    @property
    def name(self) -> str:
        return "chat_scroll"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="chat_scroll",
            description=(
                "从指定消息 ID 向上（更早）或向下（更新）翻页，浏览历史对话。\n\n"
                "适用场景：\n"
                "- 查看 chat_context 返回的消息之前更多的对话\n"
                "- 从某个关键节点继续向后追踪后续发展\n"
                "- 分段浏览长对话历史"
            ),
            parameters=[
                ToolParameter(
                    name="message_id",
                    type="integer",
                    description="锚点消息 ID",
                    required=True,
                ),
                ToolParameter(
                    name="direction",
                    type="string",
                    description='翻页方向：up（更早的消息）或 down（更新的消息），默认 "up"',
                    required=False,
                    enum=["up", "down"],
                    default="up",
                ),
                ToolParameter(
                    name="n",
                    type="integer",
                    description="返回消息条数，默认 5",
                    required=False,
                    default=5,
                ),
            ],
            examples=[
                {
                    "description": "从消息 42 继续向前查看 5 条更早的消息",
                    "params": {"message_id": 42, "direction": "up", "n": 5},
                },
                {
                    "description": "从消息 42 向后查看 3 条后续消息",
                    "params": {"message_id": 42, "direction": "down", "n": 3},
                },
            ],
            usage_notes=[
                "返回的消息按时间顺序排列（最旧到最新）",
                "direction=up 时，返回的消息 ID 均小于 message_id",
                "direction=down 时，返回的消息 ID 均大于 message_id",
            ],
            tags=["memory", "history", "navigation"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        message_id = kwargs.get("message_id")
        direction: str = kwargs.get("direction", "up")
        n: int = int(kwargs.get("n", 5))

        if message_id is None:
            return ToolResult(
                success=False,
                error="MISSING_MESSAGE_ID",
                message="必须提供 message_id",
            )

        if direction not in ("up", "down"):
            return ToolResult(
                success=False,
                error="INVALID_DIRECTION",
                message='direction 必须为 "up" 或 "down"',
            )

        try:
            message_id = int(message_id)
            results = self._db.scroll(message_id, direction=direction, n=n)
        except Exception as e:
            return ToolResult(
                success=False,
                error="SCROLL_ERROR",
                message=f"翻页失败: {e}",
            )

        dir_label = "更早" if direction == "up" else "更新"
        if not results:
            return ToolResult(
                success=True,
                data=[],
                message=f"ID {message_id} {'之前' if direction == 'up' else '之后'}没有更多消息了",
            )

        return ToolResult(
            success=True,
            data=results,
            message=f"返回 {len(results)} 条{dir_label}的消息",
        )
