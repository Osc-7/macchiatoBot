"""
记忆系统工具集

为 Agent 提供记忆检索与写入能力：
- memory_search_long_term: 在长期记忆（提炼经验）中检索
- memory_search_content: 在内容记忆（笔记、文档、PDF 等）中检索
- memory_store: 将笔记/文档文本写入内容记忆
- memory_ingest: 将文件（PDF、Word 等）转为 Markdown 存入内容记忆

说明：短期会话摘要和 MEMORY.md 已在每轮对话前自动注入 context，无需检索。
用户偏好、习惯写入 MEMORY.md 时，使用 write_file 或 modify_file 直接操作。
"""

from __future__ import annotations

from typing import Optional

from schedule_agent.core.memory import (
    ContentMemory,
    LongTermMemory,
)

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult


class MemorySearchLongTermTool(BaseTool):
    """在长期记忆中检索（提炼出的经验、决策、教训等）。"""

    def __init__(self, long_term: LongTermMemory, top_n: int = 5):
        self._long_term = long_term
        self._top_n = top_n

    @property
    def name(self) -> str:
        return "memory_search_long_term"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""在长期记忆（提炼出的经验、决策、教训、约束等）中检索。

当自动注入的 context 不足，需要补充「历史经验」「之前决策」「教训」时使用。
短期会话和 MEMORY.md 已自动注入，无需检索。""",
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description="检索查询（自然语言）",
                    required=True,
                ),
            ],
            examples=[
                {
                    "description": "搜索日程相关的历史经验",
                    "params": {"query": "日程安排策略"},
                },
            ],
            usage_notes=["返回匹配的长期记忆条目"],
            tags=['记忆', '检索'],
        )

    async def execute(self, **kwargs) -> ToolResult:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return ToolResult(
                success=False,
                error="MISSING_QUERY",
                message="请提供检索查询内容",
            )
        entries = self._long_term.search(query, self._top_n)
        if not entries:
            return ToolResult(
                success=True,
                data={"results": []},
                message="未找到相关长期记忆",
            )
        data = {
            "results": [
                {"category": e.category, "content": e.content}
                for e in entries
            ]
        }
        return ToolResult(
            success=True,
            data=data,
            message=f"找到 {len(entries)} 条相关长期记忆",
        )


class MemorySearchContentTool(BaseTool):
    """在内容记忆中检索（笔记、文档、PDF 等）。"""

    def __init__(self, content: ContentMemory, top_n: int = 5):
        self._content = content
        self._top_n = top_n

    @property
    def name(self) -> str:
        return "memory_search_content"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""在内容记忆中检索（笔记、会议记录、讲义、文档等）。

当需要查找「笔记」「文档」「会议记录」「讲义内容」时使用。
短期会话和 MEMORY.md 已自动注入，无需检索。""",
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description="检索查询（自然语言）",
                    required=True,
                ),
            ],
            examples=[
                {
                    "description": "在文档中搜索 API 相关内容",
                    "params": {"query": "API 认证"},
                },
            ],
            usage_notes=["支持关键词检索；若启用 QMD 则同时进行语义检索"],
            tags=['记忆', '检索'],
        )

    async def execute(self, **kwargs) -> ToolResult:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return ToolResult(
                success=False,
                error="MISSING_QUERY",
                message="请提供检索查询内容",
            )
        hits = self._content.search(query, self._top_n)
        qmd_hits = self._content.search_qmd(query, self._top_n)
        results = [{"path": str(p), "snippet": s[:300]} for p, s in hits]
        for hit in qmd_hits:
            path = hit.get("path", hit.get("file", "unknown"))
            snippet = hit.get("snippet", hit.get("content", ""))[:300]
            results.append({"path": str(path), "snippet": snippet})
        if not results:
            return ToolResult(
                success=True,
                data={"results": []},
                message="未找到相关内容记忆",
            )
        return ToolResult(
            success=True,
            data={"results": results},
            message=f"找到 {len(results)} 条相关内容",
        )


class MemoryStoreTool(BaseTool):
    """将文本内容写入内容记忆。"""

    def __init__(self, content_memory: ContentMemory):
        self._content = content_memory

    @property
    def name(self) -> str:
        return "memory_store"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""将文本（笔记、会议记录、文档摘要等）写入内容记忆库。

适用：用户要求保存「会议记录」「笔记」「文档总结」「讲义内容」等。
不适用：用户偏好、习惯、约束 → 使用 write_file 或 modify_file 写入 MEMORY.md。
内容会以 Markdown 格式存入 data/memory/content/，可被检索。""",
            parameters=[
                ToolParameter(
                    name="content",
                    type="string",
                    description="要保存的文本内容（Markdown 格式）",
                    required=True,
                ),
                ToolParameter(
                    name="filename",
                    type="string",
                    description="文件名（不含 .md 后缀）",
                    required=True,
                ),
                ToolParameter(
                    name="category",
                    type="string",
                    description="分类: docs | meeting | diary | lessons | notes | code | other",
                    required=False,
                    default="notes",
                    enum=["docs", "meeting", "diary", "lessons", "notes", "code", "other"],
                ),
            ],
            examples=[
                {
                    "description": "保存会议记录",
                    "params": {
                        "content": "# 周会记录\n\n讨论了 Q1 目标...",
                        "filename": "weekly-meeting-0222",
                        "category": "meeting",
                    },
                },
            ],
            usage_notes=[
                "content 建议使用 Markdown 格式",
                "文件存储在内容记忆库对应分类目录下",
            ],
            tags=['记忆', '写入'],
        )

    async def execute(self, **kwargs) -> ToolResult:
        content = str(kwargs.get("content", "")).strip()
        filename = str(kwargs.get("filename", "")).strip()
        if not content or not filename:
            return ToolResult(
                success=False,
                error="MISSING_PARAMS",
                message="content 和 filename 均为必填",
            )

        category = kwargs.get("category", "notes")
        path = self._content.ingest_text(content, filename, category)

        return ToolResult(
            success=True,
            data={"path": str(path)},
            message=f"已保存到内容记忆: {path}",
        )


class MemoryIngestTool(BaseTool):
    """将文件转为 Markdown 存入内容记忆。"""

    def __init__(self, content_memory: ContentMemory):
        self._content = content_memory

    @property
    def name(self) -> str:
        return "memory_ingest"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""将文件（PDF、Word、PPT 等）转为 Markdown 并存入内容记忆库。

适用：用户提供文件路径，要求「导入」「入库」「索引」PDF、讲义、文档等。
不适用：用户偏好、习惯 → 使用 write_file 或 modify_file 写入 MEMORY.md。
依赖 markitdown 转换，支持 PDF、DOCX、HTML、PPTX 等格式。""",
            parameters=[
                ToolParameter(
                    name="file_path",
                    type="string",
                    description="源文件路径",
                    required=True,
                ),
                ToolParameter(
                    name="category",
                    type="string",
                    description="分类: docs | meeting | diary | lessons | notes | code | other",
                    required=False,
                    default="docs",
                    enum=["docs", "meeting", "diary", "lessons", "notes", "code", "other"],
                ),
                ToolParameter(
                    name="title",
                    type="string",
                    description="自定义标题（默认使用文件名）",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "导入 PDF 讲义",
                    "params": {
                        "file_path": "/path/to/lecture.pdf",
                        "category": "docs",
                        "title": "机器学习讲义",
                    },
                },
            ],
            usage_notes=[
                "转换后的 Markdown 存储在内容记忆库对应分类目录下",
            ],
            tags=['记忆', '导入'],
        )

    async def execute(self, **kwargs) -> ToolResult:
        file_path = str(kwargs.get("file_path", "")).strip()
        if not file_path:
            return ToolResult(
                success=False,
                error="MISSING_FILE_PATH",
                message="请提供文件路径",
            )

        category = kwargs.get("category", "docs")
        title = kwargs.get("title")

        result_path = self._content.ingest_file(file_path, category, title)
        if result_path is None:
            return ToolResult(
                success=False,
                error="INGEST_FAILED",
                message=f"文件转换失败: {file_path}（文件不存在或格式不支持）",
            )

        return ToolResult(
            success=True,
            data={"path": str(result_path)},
            message=f"已导入到内容记忆: {result_path}",
        )
