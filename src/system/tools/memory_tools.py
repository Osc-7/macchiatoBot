"""
记忆系统工具集（三工具面）

- memory_store: 写入可检索语料库（文本或文件）
- memory_update: 更新 daemon 上的成体系 markdown 文档
- memory_search: 检索语料库（向量 / 关键词）

成体系文档（MEMORY.md、identity、user、soul）由 memory_update 维护并注入 system prompt，
不参与 memory_search。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from agent_core.config import Config, get_config
from agent_core.memory import LongTermMemory, MemoryCorpus
from agent_core.memory.memory_docs import resolve_memory_doc_path
from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from system.tools.file_tools import _search_replace_with_fallbacks


class MemorySearchTool(BaseTool):
    """检索统一记忆语料库。"""

    def __init__(self, corpus: MemoryCorpus, top_n: int = 5):
        self._corpus = corpus
        self._top_n = top_n

    @property
    def name(self) -> str:
        return "memory_search"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""在记忆检索库中搜索以前存入的笔记、文档、话题摘要等。

MEMORY.md、identity、user、soul 等成体系文档已自动注入 system prompt，无需检索。
当需要找回用户曾让你保存的笔记、导入的资料、或会话话题摘要时使用。""",
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
                    "description": "搜索深色模式偏好",
                    "params": {"query": "深色模式"},
                },
            ],
            usage_notes=[
                "仅搜索记忆语料库，不搜索 MEMORY.md 等 prompt 文档",
            ],
            tags=["记忆", "检索"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return ToolResult(
                success=False,
                error="MISSING_QUERY",
                message="请提供检索查询内容",
            )
        results = self._corpus.search(query, self._top_n)
        if not results:
            return ToolResult(
                success=True,
                data={"results": []},
                message="未找到相关记忆",
            )
        return ToolResult(
            success=True,
            data={"results": results},
            message=f"找到 {len(results)} 条相关记忆",
        )


class MemoryStoreTool(BaseTool):
    """写入统一记忆语料库。"""

    def __init__(self, corpus: MemoryCorpus):
        self._corpus = corpus

    @property
    def name(self) -> str:
        return "memory_store"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""将内容写入记忆检索库，之后可用 memory_search 找回。

适用：笔记、会议记录、文档摘要、用户要求记住且以后可能要搜回来的事实等。
也可传入 file_path 导入 PDF、Word、Markdown 等文件（自动转换）。

不适用：整理 MEMORY.md / identity / user / soul → 使用 memory_update。""",
            parameters=[
                ToolParameter(
                    name="content",
                    type="string",
                    description="要保存的文本内容（Markdown 格式）。与 file_path 二选一",
                    required=False,
                ),
                ToolParameter(
                    name="file_path",
                    type="string",
                    description="要导入的源文件路径（PDF、Word、md 等）。与 content 二选一",
                    required=False,
                ),
                ToolParameter(
                    name="title",
                    type="string",
                    description="标题或文件名（不含 .md 后缀）；默认从内容或源文件推导",
                    required=False,
                ),
                ToolParameter(
                    name="category",
                    type="string",
                    description="分类: docs | meeting | diary | lessons | notes | code | other",
                    required=False,
                    default="notes",
                    enum=[
                        "docs",
                        "meeting",
                        "diary",
                        "lessons",
                        "notes",
                        "code",
                        "other",
                    ],
                ),
            ],
            examples=[
                {
                    "description": "保存会议记录",
                    "params": {
                        "content": "# 周会记录\n\n讨论了 Q1 目标...",
                        "title": "weekly-meeting-0222",
                        "category": "meeting",
                    },
                },
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
                "写入后自动进入检索库，可用 memory_search 找回",
            ],
            tags=["记忆", "写入"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        content = str(kwargs.get("content", "") or "").strip()
        file_path = str(kwargs.get("file_path", "") or "").strip()
        category = str(kwargs.get("category", "notes") or "notes")

        if content and file_path:
            return ToolResult(
                success=False,
                error="AMBIGUOUS_INPUT",
                message="content 与 file_path 只能提供一个",
            )
        if not content and not file_path:
            return ToolResult(
                success=False,
                error="MISSING_INPUT",
                message="请提供 content 或 file_path",
            )

        if file_path:
            title = kwargs.get("title")
            result_path = self._corpus.store_file(file_path, category, title)
            if result_path is None:
                return ToolResult(
                    success=False,
                    error="STORE_FAILED",
                    message=f"文件导入失败: {file_path}（文件不存在或格式不支持）",
                )
            return ToolResult(
                success=True,
                data={"path": str(result_path), "searchable": True},
                message=f"已导入记忆库: {result_path}",
            )

        title = str(kwargs.get("title", "") or "").strip()
        if not title:
            snippet = re.sub(r"\s+", " ", content)[:40]
            title = snippet or f"note-{int(time.time())}"
        path = self._corpus.store_text(content, title, category)
        return ToolResult(
            success=True,
            data={"path": str(path), "searchable": True},
            message=f"已写入记忆库: {path}",
        )


class MemoryUpdateTool(BaseTool):
    """更新 daemon 上的成体系 markdown 文档。"""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()

    @property
    def name(self) -> str:
        return "memory_update"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""更新 daemon 上的成体系记忆文档（无论当前是否在远程工作区）。

可用 doc：memory（MEMORY.md）、soul、identity、user、agents。
用法与 modify_file 相同：search_replace / append / overwrite。

适用：用户要求更新长期偏好文档、人设、用户画像等。
日常「记住一条以后要搜的信息」请用 memory_store。""",
            parameters=[
                ToolParameter(
                    name="doc",
                    type="string",
                    description="文档名：memory | soul | identity | user | agents（也接受 memory/soul 形式）",
                    required=True,
                ),
                ToolParameter(
                    name="mode",
                    type="string",
                    description="修改模式：search_replace | append | overwrite",
                    required=False,
                    enum=["search_replace", "append", "overwrite"],
                    default="search_replace",
                ),
                ToolParameter(
                    name="old_text",
                    type="string",
                    description="search_replace：要查找的文本",
                    required=False,
                ),
                ToolParameter(
                    name="new_text",
                    type="string",
                    description="search_replace：替换后的文本",
                    required=False,
                ),
                ToolParameter(
                    name="content",
                    type="string",
                    description="append/overwrite：要写入的内容",
                    required=False,
                ),
                ToolParameter(
                    name="replace_all",
                    type="boolean",
                    description="search_replace：是否替换所有匹配",
                    required=False,
                    default=False,
                ),
                ToolParameter(
                    name="encoding",
                    type="string",
                    description="文件编码，默认 utf-8",
                    required=False,
                    default="utf-8",
                ),
            ],
            examples=[
                {
                    "description": "向 MEMORY.md 追加偏好",
                    "params": {
                        "doc": "memory",
                        "mode": "append",
                        "content": "\n- 偏好使用深色模式",
                    },
                },
            ],
            usage_notes=[
                "始终在 daemon 本地写入，远程 session 也生效",
                "这些文档会注入 system prompt，请保持精炼",
            ],
            tags=["记忆", "文档"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        exec_ctx = kwargs.pop("__execution_context__", None) or {}
        doc = kwargs.get("doc")
        if not doc:
            return ToolResult(
                success=False,
                error="MISSING_DOC",
                message="缺少 doc 参数",
            )

        resolved, err = resolve_memory_doc_path(str(doc), config=self._config, exec_ctx=exec_ctx)
        if err or resolved is None:
            return ToolResult(
                success=False,
                error="INVALID_DOC",
                message=err or f"无法解析 doc: {doc}",
            )

        if doc.strip().lower() == "memory" or str(doc).split("/")[-1].lower() == "memory":
            from agent_core.memory.long_term import LongTermMemory

            LongTermMemory._ensure_memory_md_on_path(resolved)

        mode = kwargs.get("mode", "search_replace")
        if mode not in ("search_replace", "append", "overwrite"):
            return ToolResult(
                success=False,
                error="INVALID_MODE",
                message="mode 必须为 search_replace、append 或 overwrite",
            )

        encoding = kwargs.get("encoding", "utf-8")
        replace_all = bool(kwargs.get("replace_all", False))

        if mode == "search_replace":
            old_text = kwargs.get("old_text")
            new_text = kwargs.get("new_text")
            if old_text is None or new_text is None:
                return ToolResult(
                    success=False,
                    error="MISSING_PARAMS",
                    message="search_replace 需要 old_text 和 new_text",
                )
            if not resolved.exists():
                return ToolResult(
                    success=False,
                    error="FILE_NOT_FOUND",
                    message=f"文档不存在: {resolved}",
                )
            try:
                file_content = resolved.read_text(encoding=encoding)
            except OSError as exc:
                return ToolResult(
                    success=False,
                    error="IO_ERROR",
                    message=f"读取失败: {exc}",
                )
            new_content, err_msg = _search_replace_with_fallbacks(
                file_content, old_text, new_text, replace_all
            )
            if err_msg:
                return ToolResult(
                    success=False,
                    error="SEARCH_REPLACE_FAILED",
                    message=err_msg,
                )
            assert new_content is not None
            try:
                resolved.write_text(new_content, encoding=encoding)
            except OSError as exc:
                return ToolResult(
                    success=False,
                    error="IO_ERROR",
                    message=f"写入失败: {exc}",
                )
            return ToolResult(
                success=True,
                data={"doc": doc, "path": str(resolved), "backend": "daemon", "mode": mode},
                message=f"已更新文档: {resolved.name}",
            )

        content = kwargs.get("content")
        if content is None:
            return ToolResult(
                success=False,
                error="MISSING_CONTENT",
                message="append/overwrite 需要 content",
            )
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append":
                with open(resolved, "a", encoding=encoding) as f:
                    f.write(content)
            else:
                resolved.write_text(content, encoding=encoding)
        except OSError as exc:
            return ToolResult(
                success=False,
                error="IO_ERROR",
                message=f"写入失败: {exc}",
            )
        return ToolResult(
            success=True,
            data={"doc": doc, "path": str(resolved), "backend": "daemon", "mode": mode},
            message=f"已更新文档: {resolved.name}",
        )


# 过渡期别名（不注册到默认工具列表）
class MemorySearchLongTermTool(MemorySearchTool):
    @property
    def name(self) -> str:
        return "memory_search_long_term"


class MemorySearchContentTool(MemorySearchTool):
    @property
    def name(self) -> str:
        return "memory_search_content"


class MemoryIngestTool(MemoryStoreTool):
    @property
    def name(self) -> str:
        return "memory_ingest"

    def get_definition(self) -> ToolDefinition:
        defn = super().get_definition()
        defn.name = self.name
        defn.description = (
            "（已合并到 memory_store）将文件转为 Markdown 写入记忆库。"
            "请优先使用 memory_store 的 file_path 参数。"
        )
        return defn

    async def execute(self, **kwargs) -> ToolResult:
        if kwargs.get("file_path") and not kwargs.get("content"):
            return await super().execute(**kwargs)
        file_path = kwargs.get("file_path", "")
        return await super().execute(
            file_path=file_path,
            category=kwargs.get("category", "docs"),
            title=kwargs.get("title"),
            __execution_context__=kwargs.get("__execution_context__"),
        )
