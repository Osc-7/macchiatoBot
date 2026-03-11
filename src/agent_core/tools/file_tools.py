"""
文件读写工具 - 提供 read_file, write_file, modify_file

路径可为任意有效路径（/etc、~/.config 等），与 command_tools 一致。
读取受 allow_read 配置控制，写入和修改需要 allow_write/allow_modify 权限。
modify_file 支持三种模式：search_replace（局部替换）、append（追加）、overwrite（覆盖）。
"""

from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from agent_core.config import Config, FileToolsConfig, get_config


class ReadFileTool(BaseTool):
    """
    读取文件工具

    读取指定文件的内容。路径必须位于配置的 base_dir 下。
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        file_tools_config: Optional[FileToolsConfig] = None,
    ):
        """
        初始化读取文件工具。

        Args:
            config: 应用配置（可选，默认使用全局配置）
            file_tools_config: 文件工具配置（可选，从 config 中获取）
        """
        self._config = config or get_config()
        self._ft_config = file_tools_config or self._config.file_tools

    @property
    def name(self) -> str:
        return "read_file"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_file",
            description="""读取指定文件的内容。

当用户想要：
- 查看某个文件的内容
- 读取笔记、文档、配置文件
- 了解文件中写了什么

工具会：
- 返回文件文本内容（UTF-8 编码）
- 对二进制文件返回错误""",
            parameters=[
                ToolParameter(
                    name="path",
                    type="string",
                    description="文件路径（相对路径相对于 base_dir，绝对路径可为 /etc/xxx、~/.config/xxx 等）",
                    required=True,
                ),
                ToolParameter(
                    name="encoding",
                    type="string",
                    description="文件编码，默认 utf-8",
                    required=False,
                    default="utf-8",
                ),
                ToolParameter(
                    name="start_line",
                    type="integer",
                    description="从第几行开始读取（从 1 开始计数），默认从第 1 行开始",
                    required=False,
                ),
                ToolParameter(
                    name="max_lines",
                    type="integer",
                    description="最多读取的行数；未提供时从 start_line 读取到文件末尾",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "读取项目中的 README 文件",
                    "params": {"path": "README.md"},
                },
                {
                    "description": "读取指定编码的文件",
                    "params": {"path": "data.txt", "encoding": "gbk"},
                },
            ],
            usage_notes=[
                "path 支持任意有效路径（/etc/xxx、~/.config/xxx 等），相对路径相对于 base_dir",
                "只能读取文本文件，二进制文件会返回错误",
                "文件不存在时返回明确错误",
                "对于大文件，建议结合 start_line 和 max_lines 分段读取，以避免一次性加载过多内容",
            ],
            tags=["文件", "读取"],
        )

    def _resolve_path(self, path: str) -> tuple[Optional[Path], Optional[str]]:
        """
        解析路径，支持任意有效路径（绝对路径直接使用，相对路径相对于 base_dir）。

        Returns:
            (resolved_path, error_message) - 成功时 error_message 为 None
        """
        base = Path(self._ft_config.base_dir).resolve()
        try:
            raw = Path(path).expanduser()
            resolved = raw.resolve() if raw.is_absolute() else (base / raw).resolve()
            return resolved, None
        except (OSError, ValueError) as e:
            return None, f"无效路径: {e}"

    async def execute(self, **kwargs) -> ToolResult:
        exec_ctx = kwargs.pop("__execution_context__", None) or {}
        path_str = kwargs.get("path")
        if not path_str:
            return ToolResult(
                success=False,
                error="MISSING_PATH",
                message="缺少必需参数: path",
            )

        # 若 path 为 MEMORY.md，则根据前端 source + user_id 映射到
        # data/memory/{frontend}/{user_id}/long_term/MEMORY.md，避免默认落到项目根目录。
        try:
            from agent_core.config import MemoryConfig  # 局部导入避免循环
            from agent_core.agent.memory_paths import resolve_memory_owner_paths

            if Path(path_str).name == "MEMORY.md":
                source = (exec_ctx.get("source") or "").strip() or "cli"
                user_id = (exec_ctx.get("user_id") or "").strip() or "root"
                mem_cfg: MemoryConfig = (
                    getattr(self._config, "memory", None) or MemoryConfig()
                )
                paths = resolve_memory_owner_paths(
                    mem_cfg, user_id, config=self._config, source=source
                )
                path_str = paths["memory_md_path"]
                kwargs["path"] = path_str
        except Exception:
            # 解析失败时退回原始 path_str 行为
            pass

        # 若 path 为 MEMORY.md，则根据前端 source + user_id 映射到
        # data/memory/{frontend}/{user_id}/long_term/MEMORY.md，避免默认落到项目根目录。
        try:
            from agent_core.config import MemoryConfig  # 局部导入避免循环
            from agent_core.agent.memory_paths import resolve_memory_owner_paths

            if Path(path_str).name == "MEMORY.md":
                source = (exec_ctx.get("source") or "").strip() or "cli"
                user_id = (exec_ctx.get("user_id") or "").strip() or "root"
                mem_cfg: MemoryConfig = (
                    getattr(self._config, "memory", None) or MemoryConfig()
                )
                paths = resolve_memory_owner_paths(
                    mem_cfg, user_id, config=self._config, source=source
                )
                path_str = paths["memory_md_path"]
                kwargs["path"] = path_str
        except Exception:
            # 解析失败时退回原始 path_str 行为
            pass

        if not self._ft_config.allow_read:
            return ToolResult(
                success=False,
                error="PERMISSION_DENIED",
                message="文件读取功能未启用，请在配置中设置 file_tools.allow_read: true",
            )

        resolved, err = self._resolve_path(path_str)
        if err:
            return ToolResult(success=False, error="INVALID_PATH", message=err)

        # 水源会话的修改路径白名单：仅允许修改本用户的长期记忆 MEMORY.md
        source = (exec_ctx.get("source") or "").strip()
        user_id = (exec_ctx.get("user_id") or "").strip()
        if source == "shuiyuan" and user_id:
            from agent_core.config import MemoryConfig  # 局部导入避免循环
            from agent_core.agent.memory_paths import resolve_memory_owner_paths

            mem_cfg: MemoryConfig = (
                getattr(self._config, "memory", None) or MemoryConfig()
            )
            paths = resolve_memory_owner_paths(
                mem_cfg, user_id, config=self._config, source="shuiyuan"
            )
            allowed_memory_md = Path(paths["memory_md_path"]).resolve()
            if resolved != allowed_memory_md:
                return ToolResult(
                    success=False,
                    error="FORBIDDEN_PATH",
                    message="水源会话仅允许修改本用户的长期记忆文件 MEMORY.md。",
                )

        # frontend 级别的路径白名单控制：水源会话只允许读取自己的长期记忆 MEMORY.md
        source = (exec_ctx.get("source") or "").strip()
        user_id = (exec_ctx.get("user_id") or "").strip()
        if source == "shuiyuan" and user_id:
            from agent_core.config import MemoryConfig  # 局部导入避免循环
            from agent_core.agent.memory_paths import resolve_memory_owner_paths

            mem_cfg: MemoryConfig = (
                getattr(self._config, "memory", None) or MemoryConfig()
            )
            paths = resolve_memory_owner_paths(
                mem_cfg, user_id, config=self._config, source="shuiyuan"
            )
            allowed_memory_md = Path(paths["memory_md_path"]).resolve()
            if resolved != allowed_memory_md:
                return ToolResult(
                    success=False,
                    error="FORBIDDEN_PATH",
                    message="水源会话仅允许读取本用户的长期记忆文件 MEMORY.md。",
                )

        encoding = kwargs.get("encoding", "utf-8")
        start_line = kwargs.get("start_line")
        max_lines = kwargs.get("max_lines")

        # 校验 start_line / max_lines（如提供）
        if start_line is not None:
            try:
                start_line = int(start_line)
            except (TypeError, ValueError):
                return ToolResult(
                    success=False,
                    error="INVALID_START_LINE",
                    message="start_line 必须为正整数",
                )
            if start_line < 1:
                return ToolResult(
                    success=False,
                    error="INVALID_START_LINE",
                    message="start_line 必须为正整数",
                )

        if max_lines is not None:
            try:
                max_lines = int(max_lines)
            except (TypeError, ValueError):
                return ToolResult(
                    success=False,
                    error="INVALID_MAX_LINES",
                    message="max_lines 必须为正整数",
                )
            if max_lines < 1:
                return ToolResult(
                    success=False,
                    error="INVALID_MAX_LINES",
                    message="max_lines 必须为正整数",
                )

        try:
            content = resolved.read_text(encoding=encoding)
            # 若未指定行范围，保持向后兼容：返回完整内容
            if start_line is None and max_lines is None:
                return ToolResult(
                    success=True,
                    data={"path": str(resolved), "content": content},
                    message=f"成功读取文件: {resolved.name}",
                )

            # 按行截取
            lines = content.splitlines()
            total_lines = len(lines)
            start = start_line if start_line is not None else 1
            # Python 索引从 0 开始
            start_idx = max(0, start - 1)

            if max_lines is None:
                end_idx = total_lines
            else:
                end_idx = min(total_lines, start_idx + max_lines)

            sliced_lines = lines[start_idx:end_idx]
            sliced_content = "\n".join(sliced_lines)

            return ToolResult(
                success=True,
                data={
                    "path": str(resolved),
                    "content": sliced_content,
                    "start_line": start,
                    "max_lines": max_lines
                    if max_lines is not None
                    else total_lines - start_idx,
                    "total_lines": total_lines,
                    "has_more": end_idx < total_lines,
                },
                message=f"成功读取文件: {resolved.name}（第 {start} 行起，共返回 {len(sliced_lines)} 行）",
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                error="FILE_NOT_FOUND",
                message=f"文件不存在: {path_str}",
            )
        except UnicodeDecodeError as e:
            return ToolResult(
                success=False,
                error="ENCODING_ERROR",
                message=f"无法以 {encoding} 编码读取文件，可能是二进制文件: {e}",
            )
        except OSError as e:
            return ToolResult(
                success=False,
                error="IO_ERROR",
                message=f"读取失败: {e}",
            )


class WriteFileTool(BaseTool):
    """
    写入文件工具

    创建新文件或覆盖已有文件。需要权限控制（配置或回调确认）。
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        file_tools_config: Optional[FileToolsConfig] = None,
        permission_provider: Optional[
            Callable[[str, str, str], Awaitable[bool]]
        ] = None,
    ):
        self._config = config or get_config()
        self._ft_config = file_tools_config or self._config.file_tools
        self._permission_provider = permission_provider

    @property
    def name(self) -> str:
        return "write_file"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="write_file",
            description="""创建新文件或覆盖已有文件。

当用户想要：
- 创建新文件并写入内容
- 保存笔记、配置、代码片段
- 覆盖现有文件

工具会：
- 检查写入权限（配置或用户确认）
- 创建或覆盖文件
- 若文件已存在会完全覆盖原内容""",
            parameters=[
                ToolParameter(
                    name="path",
                    type="string",
                    description="文件路径（相对路径相对于 base_dir，绝对路径可为任意有效路径）",
                    required=True,
                ),
                ToolParameter(
                    name="content",
                    type="string",
                    description="要写入的完整文本内容",
                    required=True,
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
                    "description": "创建并写入新文件",
                    "params": {
                        "path": "notes.txt",
                        "content": "今日待办：1. 开会 2. 写报告",
                    },
                },
            ],
            usage_notes=[
                "写入和覆盖需要配置允许：file_tools.allow_write: true",
                "path 支持任意有效路径（/etc、~/.config 等）",
                "会完全覆盖已存在的文件",
            ],
            tags=["文件", "写入"],
        )

    def _resolve_path(self, path: str) -> tuple[Optional[Path], Optional[str]]:
        base = Path(self._ft_config.base_dir).resolve()
        try:
            raw = Path(path).expanduser()
            resolved = raw.resolve() if raw.is_absolute() else (base / raw).resolve()
            return resolved, None
        except (OSError, ValueError) as e:
            return None, f"无效路径: {e}"

    async def execute(self, **kwargs) -> ToolResult:
        # select mode 下禁止写入（破坏性操作）
        exec_ctx = kwargs.pop("__execution_context__", None) or {}
        if (exec_ctx.get("tool_mode") or "kernel").lower() == "select":
            return ToolResult(
                success=False,
                error="PERMISSION_DENIED",
                message="select mode 下禁止 write_file（破坏性操作）",
            )

        path_str = kwargs.get("path")
        content = kwargs.get("content")
        if not path_str:
            return ToolResult(
                success=False,
                error="MISSING_PATH",
                message="缺少必需参数: path",
            )
        if content is None:
            return ToolResult(
                success=False,
                error="MISSING_CONTENT",
                message="缺少必需参数: content",
            )

        # 若 path 为 MEMORY.md，则根据前端 source + user_id 映射到
        # data/memory/{frontend}/{user_id}/long_term/MEMORY.md，避免默认落到项目根目录。
        try:
            from agent_core.config import MemoryConfig  # 局部导入避免循环
            from agent_core.agent.memory_paths import resolve_memory_owner_paths

            if Path(path_str).name == "MEMORY.md":
                source = (exec_ctx.get("source") or "").strip() or "cli"
                user_id = (exec_ctx.get("user_id") or "").strip() or "root"
                mem_cfg: MemoryConfig = (
                    getattr(self._config, "memory", None) or MemoryConfig()
                )
                paths = resolve_memory_owner_paths(
                    mem_cfg, user_id, config=self._config, source=source
                )
                path_str = paths["memory_md_path"]
                kwargs["path"] = path_str
        except Exception:
            # 解析失败时退回原始 path_str 行为
            pass

        if not self._ft_config.allow_write:
            return ToolResult(
                success=False,
                error="PERMISSION_DENIED",
                message="文件写入功能未启用。请在配置中设置 file_tools.allow_write: true",
            )

        resolved, err = self._resolve_path(path_str)
        if err:
            return ToolResult(success=False, error="INVALID_PATH", message=err)

        # 水源会话的写入路径白名单：仅允许写入本用户的长期记忆 MEMORY.md
        source = (exec_ctx.get("source") or "").strip()
        user_id = (exec_ctx.get("user_id") or "").strip()
        if source == "shuiyuan" and user_id:
            from agent_core.config import MemoryConfig  # 局部导入避免循环
            from agent_core.agent.memory_paths import resolve_memory_owner_paths

            mem_cfg: MemoryConfig = (
                getattr(self._config, "memory", None) or MemoryConfig()
            )
            paths = resolve_memory_owner_paths(
                mem_cfg, user_id, config=self._config, source="shuiyuan"
            )
            allowed_memory_md = Path(paths["memory_md_path"]).resolve()
            if resolved != allowed_memory_md:
                return ToolResult(
                    success=False,
                    error="FORBIDDEN_PATH",
                    message="水源会话仅允许写入本用户的长期记忆文件 MEMORY.md。",
                )

        # 若有权限提供者，则调用确认
        if self._permission_provider:
            summary = f"写入 {path_str}，共 {len(content)} 字符"
            allowed = await self._permission_provider("write", path_str, summary)
            if not allowed:
                return ToolResult(
                    success=False,
                    error="USER_DENIED",
                    message="用户拒绝了写入操作",
                )

        encoding = kwargs.get("encoding", "utf-8")

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding=encoding)
            return ToolResult(
                success=True,
                data={"path": str(resolved)},
                message=f"成功写入文件: {resolved.name}",
            )
        except OSError as e:
            return ToolResult(
                success=False,
                error="IO_ERROR",
                message=f"写入失败: {e}",
            )


def _search_replace_with_fallbacks(
    content: str, old_text: str, new_text: str, replace_all: bool
) -> Tuple[Optional[str], Optional[str]]:
    """
    多级回退的 search-replace（参考 Cline、OpenCode、Aider 实践）。
    Returns: (new_content, None) 成功； (None, error_message) 失败。
    """
    if not old_text:
        return None, "old_text 不能为空"

    # 1. 精确匹配
    if replace_all:
        if old_text in content:
            return content.replace(old_text, new_text), None
    else:
        if old_text in content:
            return content.replace(old_text, new_text, 1), None

    # 2. 行级 trim 容差：按行 rstrip 后逐行比对
    old_lines = old_text.splitlines()
    content_lines = content.splitlines()
    if old_lines and len(content_lines) >= len(old_lines):
        old_stripped = [line.rstrip() for line in old_lines]
        i = 0
        replaced = False
        while i <= len(content_lines) - len(old_lines):
            match = all(
                content_lines[i + j].rstrip() == old_stripped[j]
                for j in range(len(old_lines))
            )
            if match:
                block = "\n".join(content_lines[i : i + len(old_lines)])
                new_content = content.replace(block, new_text, 1)
                if not replace_all:
                    return new_content, None
                content = new_content
                content_lines = content.splitlines()
                i = 0
                replaced = True
            else:
                i += 1
        if replaced:
            return content, None

    # 3. 锚点匹配：用首尾行定位块（块至少 2 行）
    old_stripped = [
        line.strip() for line in old_text.strip().splitlines() if line.strip()
    ]
    if len(old_stripped) >= 2:
        first_line = old_stripped[0]
        last_line = old_stripped[-1]
        start_idx = None
        end_idx = None
        for i, line in enumerate(content_lines):
            if line.strip() == first_line:
                start_idx = i
                break
        if start_idx is not None:
            for i in range(len(content_lines) - 1, start_idx, -1):
                if content_lines[i].strip() == last_line:
                    end_idx = i
                    break
        if start_idx is not None and end_idx is not None and end_idx >= start_idx:
            block = "\n".join(content_lines[start_idx : end_idx + 1])
            if replace_all:
                new_content = content.replace(block, new_text)
            else:
                new_content = content.replace(block, new_text, 1)
            if new_content != content:
                return new_content, None

    return None, (
        "未找到匹配的 old_text。建议：1) 确认 old_text 与文件内容完全一致（含缩进、换行）；"
        "2) 使用 read_file 读取后再用 write_file 覆盖。"
    )


class ModifyFileTool(BaseTool):
    """
    修改文件工具（符合 AI Coding Agent 最佳实践）

    支持三种模式：
    - search_replace: 局部替换（多级回退匹配），Token 高效，适合精确修改
    - append: 在文件末尾追加
    - overwrite: 覆盖整个文件（与 write_file 类似）
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        file_tools_config: Optional[FileToolsConfig] = None,
        permission_provider: Optional[
            Callable[[str, str, str], Awaitable[bool]]
        ] = None,
    ):
        self._config = config or get_config()
        self._ft_config = file_tools_config or self._config.file_tools
        self._permission_provider = permission_provider

    @property
    def name(self) -> str:
        return "modify_file"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="modify_file",
            description="""修改现有文件。支持三种模式，按场景选择：

1. **search_replace**（推荐）：局部替换，只需提供 old_text 和 new_text，Token 成本低。
   适合：修改函数、插入几行、替换配置项等。支持多级回退匹配（精确→空白容差→锚点）。
2. **append**：在文件末尾追加内容。适合：日志、MEMORY.md、笔记追加。
3. **overwrite**：覆盖整个文件。适合：小文件重写；search_replace 多次失败时的兜底。

优先使用 search_replace；大范围修改或匹配失败时，用 read_file 读取后用 write_file 覆盖。""",
            parameters=[
                ToolParameter(
                    name="path",
                    type="string",
                    description="文件路径（相对路径相对于 base_dir，绝对路径可为任意有效路径）",
                    required=True,
                ),
                ToolParameter(
                    name="mode",
                    type="string",
                    description="修改模式：search_replace（局部替换）| append（追加）| overwrite（覆盖）",
                    required=False,
                    enum=["search_replace", "append", "overwrite"],
                    default="search_replace",
                ),
                ToolParameter(
                    name="old_text",
                    type="string",
                    description="search_replace 模式：要查找并替换的文本。需与文件内容一致（含缩进、换行）",
                    required=False,
                ),
                ToolParameter(
                    name="new_text",
                    type="string",
                    description="search_replace 模式：替换后的文本",
                    required=False,
                ),
                ToolParameter(
                    name="content",
                    type="string",
                    description="append/overwrite 模式：要追加或写入的完整内容",
                    required=False,
                ),
                ToolParameter(
                    name="replace_all",
                    type="boolean",
                    description="search_replace 模式：是否替换所有匹配（默认 false，仅替换第一次）",
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
                    "description": "局部替换：在函数中插入一行",
                    "params": {
                        "path": "app.py",
                        "mode": "search_replace",
                        "old_text": "def greet():\n    pass",
                        "new_text": "def greet():\n    print('hi')\n    pass",
                    },
                },
                {
                    "description": "在文件末尾追加",
                    "params": {
                        "path": "MEMORY.md",
                        "mode": "append",
                        "content": "\n- 用户偏好下午开会",
                    },
                },
                {
                    "description": "覆盖整个文件（兜底）",
                    "params": {
                        "path": "config.txt",
                        "mode": "overwrite",
                        "content": "新配置内容",
                    },
                },
            ],
            usage_notes=[
                "修改需要配置允许：file_tools.allow_modify: true",
                "path 支持任意有效路径（/etc、~/.config 等）",
                "search_replace 时 old_text 需与文件内容完全一致；失败时建议 read_file + write_file",
                "append 模式下文件不存在会先创建",
            ],
            tags=["文件", "修改"],
        )

    def _resolve_path(self, path: str) -> tuple[Optional[Path], Optional[str]]:
        base = Path(self._ft_config.base_dir).resolve()
        try:
            raw = Path(path).expanduser()
            resolved = raw.resolve() if raw.is_absolute() else (base / raw).resolve()
            return resolved, None
        except (OSError, ValueError) as e:
            return None, f"无效路径: {e}"

    async def execute(self, **kwargs) -> ToolResult:
        # select mode 下禁止修改（破坏性操作）
        exec_ctx = kwargs.pop("__execution_context__", None) or {}
        if (exec_ctx.get("tool_mode") or "kernel").lower() == "select":
            return ToolResult(
                success=False,
                error="PERMISSION_DENIED",
                message="select mode 下禁止 modify_file（破坏性操作）",
            )

        path_str = kwargs.get("path")
        if not path_str:
            return ToolResult(
                success=False,
                error="MISSING_PATH",
                message="缺少必需参数: path",
            )

        if not self._ft_config.allow_modify:
            return ToolResult(
                success=False,
                error="PERMISSION_DENIED",
                message="文件修改功能未启用。请在配置中设置 file_tools.allow_modify: true",
            )

        resolved, err = self._resolve_path(path_str)
        if err:
            return ToolResult(success=False, error="INVALID_PATH", message=err)

        mode = kwargs.get("mode", "search_replace")
        if mode not in ("search_replace", "append", "overwrite"):
            return ToolResult(
                success=False,
                error="INVALID_MODE",
                message="mode 必须为 search_replace、append 或 overwrite",
            )

        encoding = kwargs.get("encoding", "utf-8")
        replace_all = bool(kwargs.get("replace_all", False))

        # search_replace 模式
        if mode == "search_replace":
            old_text = kwargs.get("old_text")
            new_text = kwargs.get("new_text")
            if old_text is None or new_text is None:
                return ToolResult(
                    success=False,
                    error="MISSING_PARAMS",
                    message="search_replace 模式需要 old_text 和 new_text",
                )
            if not resolved.exists():
                return ToolResult(
                    success=False,
                    error="FILE_NOT_FOUND",
                    message=f"文件不存在: {path_str}，search_replace 只能修改已存在的文件",
                )

            try:
                content = resolved.read_text(encoding=encoding)
            except OSError as e:
                return ToolResult(
                    success=False,
                    error="IO_ERROR",
                    message=f"读取失败: {e}",
                )

            new_content, err_msg = _search_replace_with_fallbacks(
                content, old_text, new_text, replace_all
            )
            if err_msg:
                return ToolResult(
                    success=False,
                    error="SEARCH_REPLACE_FAILED",
                    message=err_msg,
                )

            if self._permission_provider:
                summary = (
                    f"search_replace {path_str}，替换 1 处"
                    if not replace_all
                    else f"search_replace {path_str}，替换多处"
                )
                allowed = await self._permission_provider("modify", path_str, summary)
                if not allowed:
                    return ToolResult(
                        success=False,
                        error="USER_DENIED",
                        message="用户拒绝了修改操作",
                    )

            try:
                resolved.write_text(new_content, encoding=encoding)
                return ToolResult(
                    success=True,
                    data={"path": str(resolved), "mode": "search_replace"},
                    message=f"成功局部替换文件: {resolved.name}",
                )
            except OSError as e:
                return ToolResult(
                    success=False,
                    error="IO_ERROR",
                    message=f"写入失败: {e}",
                )

        # append / overwrite 模式
        content = kwargs.get("content")
        if content is None:
            return ToolResult(
                success=False,
                error="MISSING_CONTENT",
                message="append 和 overwrite 模式需要 content 参数",
            )

        action_cn = "追加" if mode == "append" else "覆盖"
        if self._permission_provider:
            summary = f"{action_cn} {path_str}，共 {len(content)} 字符"
            allowed = await self._permission_provider("modify", path_str, summary)
            if not allowed:
                return ToolResult(
                    success=False,
                    error="USER_DENIED",
                    message="用户拒绝了修改操作",
                )

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append":
                with open(resolved, "a", encoding=encoding) as f:
                    f.write(content)
            else:
                resolved.write_text(content, encoding=encoding)
            return ToolResult(
                success=True,
                data={"path": str(resolved), "mode": mode},
                message=f"成功{action_cn}文件: {resolved.name}",
            )
        except OSError as e:
            return ToolResult(
                success=False,
                error="IO_ERROR",
                message=f"修改失败: {e}",
            )
