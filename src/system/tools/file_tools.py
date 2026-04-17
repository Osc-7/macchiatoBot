"""
文件读写工具 - 提供 read_file, write_file, modify_file

读取受 allow_read 配置控制；写入和修改需要 allow_write/allow_modify 权限。
开启工作区隔离时，相对路径默认落在当前 frontend/user 的工作区；
``~/`` 与单独的 ``~`` 与 bash 一致，解析为**用户单元格根目录**（与 ``MACCHIATO_WORKSPACE_ROOT`` 相同，不嵌套 ``.sandbox_home``）。
写入/修改允许发生在该工作区、临时目录 `/tmp/macchiato/{frontend}/{user}/`、
真实 `data/memory/{frontend}/{user}/`（与配置/ACL 额外可写根）内；其他路径保持只读。
需写真实用户主目录时请使用绝对路径（例如 ``$MACCHIATO_REAL_HOME`` 在 shell 中的展开值）。
modify_file 支持三种模式：search_replace（局部替换）、append（追加）、overwrite（覆盖）。
"""

from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from agent_core.config import Config, FileToolsConfig, get_config
from agent_core.agent.session_paths import expand_user_path_str_for_session
from agent_core.agent.tool_path_resolution import (
    resolve_path_string_for_tool,
    resolve_workspace_root_for_exec_ctx,
)

# 工作区隔离下 ``data/memory`` 已嫁接至当前用户的 canonical owner（见 workspace_paths），
# 相对工作区应使用此路径，勿再写 ``data/memory/{frontend}/{user}/...`` 以免路径重复一层。
_MEMORY_MD_WORKSPACE_RELATIVE = "data/memory/long_term/MEMORY.md"


def _redirect_memory_md_if_needed(path_str: str, exec_ctx: dict, config: Config) -> str:
    """将任意指向 MEMORY.md 的路径映射到当前用户的长期记忆文件。

    - **未**开启工作区隔离：解析为仓库根下 ``data/memory/{frontend}/{user}/long_term/MEMORY.md``
     （``memory_owner`` 等由 ``resolve_memory_owner_paths`` 决定）。
    - **开启**工作区隔离：使用 ``data/memory/long_term/MEMORY.md``，由工作区根下的
      ``data/memory`` symlink 解析到同一 canonical 文件。
    """
    if Path(path_str).name != "MEMORY.md":
        return path_str
    try:
        if getattr(config.command_tools, "workspace_isolation_enabled", False):
            return _MEMORY_MD_WORKSPACE_RELATIVE
        from agent_core.config import MemoryConfig
        from agent_core.agent.memory_paths import (
            effective_memory_namespace_from_execution_context,
            resolve_memory_owner_paths,
        )

        src, uid = effective_memory_namespace_from_execution_context(exec_ctx)
        mem_cfg = getattr(config, "memory", None) or MemoryConfig()
        return resolve_memory_owner_paths(mem_cfg, uid, config=config, source=src)[
            "memory_md_path"
        ]
    except Exception:
        return path_str


def _sub_mode_forbids_file_mutation(exec_ctx: dict) -> bool:
    """sub Core 统一禁止写/改文件。"""
    return (exec_ctx.get("profile_mode") or "full").lower() == "sub"


def _resolve_workspace_tmp_root_for_exec_ctx(exec_ctx: dict, config: Config) -> Path:
    """当前 frontend/user 的临时目录根。"""
    from agent_core.agent.memory_paths import (
        effective_memory_namespace_from_execution_context,
    )
    from agent_core.agent.workspace_paths import resolve_workspace_tmp_dir

    src, uid = effective_memory_namespace_from_execution_context(exec_ctx)
    return Path(
        resolve_workspace_tmp_dir(config.command_tools, uid, source=src)
    ).expanduser().resolve()


def _is_path_within_root(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _resolve_mutation_path_for_file_tool(
    path_str: str,
    exec_ctx: dict,
    ft_cfg: FileToolsConfig,
    config: Config,
) -> tuple[Optional[Path], Optional[str]]:
    """
    解析写入/修改路径。开启工作区隔离时，仅允许当前 frontend/user 的工作区内路径。
    """
    resolved, err = resolve_path_string_for_tool(
        path_str, config, exec_ctx, file_tools_config=ft_cfg
    )
    if err or resolved is None:
        return resolved, err
    if getattr(config.command_tools, "workspace_isolation_enabled", False):
        workspace_root = resolve_workspace_root_for_exec_ctx(exec_ctx, config)
        tmp_root = _resolve_workspace_tmp_root_for_exec_ctx(exec_ctx, config)
        from agent_core.agent.memory_paths import (
            effective_memory_namespace_from_execution_context,
        )
        from agent_core.agent.workspace_paths import merged_bash_write_root_paths

        src, uid = effective_memory_namespace_from_execution_context(exec_ctx)
        extras = merged_bash_write_root_paths(
            config.command_tools,
            src,
            uid,
            app_config=config,
        )
        allowed = [workspace_root, tmp_root] + extras
        if not any(_is_path_within_root(resolved, r) for r in allowed):
            return None, (
                "文件写入/修改仅允许在当前工作区、临时目录或可写白名单内（含 canonical memory）"
            )
    return resolved, None


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
                "开启工作区隔离时，相对路径相对于当前 frontend/user 的工作区；绝对路径仍可读取任意位置",
                "只能读取文本文件，二进制文件会返回错误",
                "文件不存在时返回明确错误",
                "对于大文件，建议结合 start_line 和 max_lines 分段读取，以避免一次性加载过多内容",
            ],
            tags=["文件", "读取"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        exec_ctx = kwargs.pop("__execution_context__", None) or {}
        path_str = kwargs.get("path")
        if not path_str:
            return ToolResult(
                success=False,
                error="MISSING_PATH",
                message="缺少必需参数: path",
            )

        path_str = _redirect_memory_md_if_needed(path_str, exec_ctx, self._config)
        kwargs["path"] = path_str

        if not self._ft_config.allow_read:
            return ToolResult(
                success=False,
                error="PERMISSION_DENIED",
                message="文件读取功能未启用，请在配置中设置 file_tools.allow_read: true",
            )

        resolved, err = resolve_path_string_for_tool(
            path_str, self._config, exec_ctx, file_tools_config=self._ft_config
        )
        if err:
            return ToolResult(
                success=False,
                error="INVALID_PATH",
                message=err,
            )
        if resolved is None:
            return ToolResult(
                success=False,
                error="INVALID_PATH",
                message=f"无效路径: {path_str}",
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
                "开启工作区隔离时，仅允许写入当前 frontend/user 的工作区或 `/tmp/macchiato/{frontend}/{user}/`；其他路径保持只读",
                "工作区隔离下 ``~/`` 与 bash 一致，指向该用户单元格根目录（与 ``MACCHIATO_WORKSPACE_ROOT`` 一致）",
                "会完全覆盖已存在的文件",
            ],
            tags=["文件", "写入"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        exec_ctx = kwargs.pop("__execution_context__", None) or {}
        if _sub_mode_forbids_file_mutation(exec_ctx):
            return ToolResult(
                success=False,
                error="PERMISSION_DENIED",
                message="sub 模式下禁止 write_file（破坏性操作）",
            )

        path_str = kwargs.get("path")
        content = kwargs.get("content")
        if not path_str:
            return ToolResult(
                success=False,
                error="MISSING_PATH",
                message="缺少必需参数: path。若为流式输出，可能是参数被截断导致，请重试并确保传入 path 与 content。",
            )
        if content is None:
            return ToolResult(
                success=False,
                error="MISSING_CONTENT",
                message="缺少必需参数: content",
            )

        path_str = _redirect_memory_md_if_needed(path_str, exec_ctx, self._config)
        kwargs["path"] = path_str

        if not self._ft_config.allow_write:
            return ToolResult(
                success=False,
                error="PERMISSION_DENIED",
                message="文件写入功能未启用。请在配置中设置 file_tools.allow_write: true",
            )

        resolved, err = _resolve_mutation_path_for_file_tool(
            path_str, exec_ctx, self._ft_config, self._config
        )
        if err:
            return ToolResult(
                success=False,
                error=(
                    "FORBIDDEN_PATH"
                    if (
                        "工作区内" in err
                        or "工作区或临时目录内" in err
                        or "可写白名单" in err
                        or "canonical memory" in err
                    )
                    else "INVALID_PATH"
                ),
                message=err,
            )
        if resolved is None:
            return ToolResult(
                success=False,
                error="INVALID_PATH",
                message=f"无效路径: {path_str}",
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
2. **append**：在文件末尾追加内容。适合：日志、笔记追加。
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
                        "path": "notes.md",
                        "mode": "append",
                        "content": "\n- 今日新增想法",
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
                "开启工作区隔离时，仅允许修改当前 frontend/user 的工作区或 `/tmp/macchiato/{frontend}/{user}/`；其他路径保持只读",
                "search_replace 时 old_text 需与文件内容完全一致；失败时建议 read_file + write_file",
                "append 模式下文件不存在会先创建",
            ],
            tags=["文件", "修改"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        exec_ctx = kwargs.pop("__execution_context__", None) or {}
        if _sub_mode_forbids_file_mutation(exec_ctx):
            return ToolResult(
                success=False,
                error="PERMISSION_DENIED",
                message="sub 模式下禁止 modify_file（破坏性操作）",
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

        path_str = _redirect_memory_md_if_needed(path_str, exec_ctx, self._config)
        kwargs["path"] = path_str

        resolved, err = _resolve_mutation_path_for_file_tool(
            path_str, exec_ctx, self._ft_config, self._config
        )
        if err:
            return ToolResult(
                success=False,
                error=(
                    "FORBIDDEN_PATH"
                    if (
                        "工作区内" in err
                        or "工作区或临时目录内" in err
                        or "可写白名单" in err
                        or "canonical memory" in err
                    )
                    else "INVALID_PATH"
                ),
                message=err,
            )
        if resolved is None:
            return ToolResult(
                success=False,
                error="INVALID_PATH",
                message=f"无效路径: {path_str}",
            )

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
            assert new_content is not None

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
