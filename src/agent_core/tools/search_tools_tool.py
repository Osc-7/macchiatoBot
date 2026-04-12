"""
工具搜索工具。

给 LLM 提供按需发现能力：先搜索，再调用。
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, TYPE_CHECKING

from agent_core.mcp.proxy_tool import MCPProxyTool

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from .versioned_registry import VersionedToolRegistry

if TYPE_CHECKING:
    from agent_core.orchestrator import ToolWorkingSetManager


class SearchToolsTool(BaseTool):
    """搜索工具库并更新工作集。"""

    def __init__(
        self,
        registry: VersionedToolRegistry,
        working_set: "ToolWorkingSetManager",
        profile_getter: Optional[Callable[[], Any]] = None,
    ):
        self._registry = registry
        self._working_set = working_set
        self._profile_getter = profile_getter

    @property
    def name(self) -> str:
        return "search_tools"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "在完整工具库中搜索可用工具（含进程内原生工具 + 已连接的 MCP 代理工具）。"
                "当你需要当前看不到的能力时，先搜索再使用 call_tool 按「注册名」执行。"
                "MCP 工具名一般为「前缀.远端名」，如 tavily.xxx、discourse.xxx（取决于配置里的 name / tool_name_prefix）。"
            ),
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description=(
                        "自然语言查询，例如：创建日程、查询任务、联网搜索、论坛发帖。"
                        "可为空，若与 tags / name_prefix 组合使用"
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="tags",
                    type="array",
                    description="按标签筛选，如 ['日程','查询']、['任务','规划']、['文件','读取'] 等。可与 query 组合使用",
                    required=False,
                ),
                ToolParameter(
                    name="name_prefix",
                    type="string",
                    description=(
                        "仅返回工具名以此前缀开头的条目，例如 tavily. 或 discourse.；"
                        "已知 MCP 命名空间时可直接缩小范围，不必猜全名"
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="limit",
                    type="integer",
                    description="返回数量上限，默认 8，最大 20",
                    required=False,
                    default=8,
                ),
            ],
            usage_notes=[
                "query、tags、name_prefix 至少提供一个；可组合使用。",
                "检索会同时扫描述、参数、usage_notes、以及工具自带 tags 文本；并做名称子串与中日文子片弱命中。",
                "若某条带 weak_match=true，表示强关键词未命中，仅为名称/描述相似度或字典序兜底，调用前请核对是否真需要。",
                "返回项含 tool_source：native=进程内工具，mcp=外部 MCP 代理；mcp_server 为 MCP 配置中的 server 名（若有）。",
                "若 callable_in_current_core 为 false，说明当前 CoreProfile 不允许调用。",
                "命中工具会加入当前会话工具工作集（LRU）。",
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query") or "").strip()
        name_prefix = str(kwargs.get("name_prefix") or "").strip()
        tags_raw = kwargs.get("tags")
        tags: Optional[List[str]] = None
        if tags_raw is not None:
            if isinstance(tags_raw, list):
                tags = [str(t).strip() for t in tags_raw if t]
            elif isinstance(tags_raw, str):
                tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        if not query and not tags and not name_prefix:
            return ToolResult(
                success=False,
                error="INVALID_ARGUMENTS",
                message="query、tags、name_prefix 至少需提供一个",
            )

        limit_raw = kwargs.get("limit", 8)
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(limit, 20))

        exclude_names: List[str] = [self.name]
        matches = self._registry.search(
            query=query,
            limit=limit,
            exclude_names=exclude_names,
            tags=tags,
            name_prefix=name_prefix or None,
        )
        profile = self._profile_getter() if self._profile_getter is not None else None
        visible_names: List[str] = []
        enriched: List[dict[str, Any]] = []
        for item in matches:
            name = str(item.get("name") or "").strip()
            inst = self._registry.get(name)
            tool_source = "mcp" if isinstance(inst, MCPProxyTool) else "native"
            mcp_server = (
                getattr(inst, "_server_name", None)
                if isinstance(inst, MCPProxyTool)
                else None
            )
            callable_in_current_core = (
                True if profile is None else bool(profile.is_tool_allowed(name))
            )
            if callable_in_current_core and name:
                visible_names.append(name)
            enriched.append(
                {
                    **item,
                    "tool_source": tool_source,
                    "mcp_server": mcp_server,
                    "callable_in_current_core": callable_in_current_core,
                    "reason_if_denied": (
                        None
                        if callable_in_current_core
                        else "工具不在当前 Core 的权限范围内"
                    ),
                }
            )

        self._working_set.add_to_working_set(visible_names)

        return ToolResult(
            success=True,
            data={
                "query": query,
                "name_prefix": name_prefix or None,
                "tags": tags,
                "count": len(enriched),
                "tools": enriched,
            },
            message=f"已找到 {len(enriched)} 个相关工具",
        )
