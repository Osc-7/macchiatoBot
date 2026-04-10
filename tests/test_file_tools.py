"""
文件读写工具测试 - 测试 read_file, write_file, modify_file
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_core.config import (
    Config,
    LLMConfig,
    FileToolsConfig,
    CommandToolsConfig,
    MemoryConfig,
)
from system.tools.file_tools import ReadFileTool, WriteFileTool, ModifyFileTool
from agent_core.tools.base import ToolDefinition


def _make_config(
    allow_read: bool = True,
    allow_write: bool = False,
    allow_modify: bool = False,
    base_dir: str = ".",
) -> Config:
    return Config(
        llm=LLMConfig(api_key="test", model="test"),
        file_tools=FileToolsConfig(
            enabled=True,
            allow_read=allow_read,
            allow_write=allow_write,
            allow_modify=allow_modify,
            base_dir=base_dir,
        ),
        command_tools=CommandToolsConfig(
            base_dir=base_dir,
            workspace_isolation_enabled=False,
        ),
    )


def _make_workspace_sandbox_config(
    tmp_path,
    *,
    source: str = "cli",
    allow_read: bool = True,
    allow_write: bool = True,
    allow_modify: bool = True,
) -> Config:
    """通用工作区沙箱：相对路径落在 data/workspace/{frontend}/{user}/。"""
    ws_parent = tmp_path / "workspace_parent"
    mem_base = tmp_path / "memory_parent"
    return Config(
        llm=LLMConfig(api_key="test", model="test"),
        memory=MemoryConfig(memory_base_dir=str(mem_base)),
        file_tools=FileToolsConfig(
            enabled=True,
            allow_read=allow_read,
            allow_write=allow_write,
            allow_modify=allow_modify,
            base_dir=str(tmp_path),
        ),
        command_tools=CommandToolsConfig(
            base_dir=str(tmp_path),
            workspace_base_dir=str(ws_parent),
            workspace_isolation_enabled=True,
        ),
    )


def _ctx(source: str = "cli", user_id: str = "u1", *, sub: bool = False) -> dict:
    ctx = {"source": source, "user_id": user_id}
    if sub:
        ctx["tool_mode"] = "sub"
    return ctx


# ============================================================================
# ReadFileTool
# ============================================================================


class TestReadFileTool:
    def test_get_definition(self):
        config = _make_config()
        tool = ReadFileTool(config=config)
        defn = tool.get_definition()
        assert isinstance(defn, ToolDefinition)
        assert defn.name == "read_file"
        param_names = [p.name for p in defn.parameters]
        assert "path" in param_names
        assert "encoding" in param_names
        # 新增的分页参数
        assert "start_line" in param_names
        assert "max_lines" in param_names

    @pytest.mark.asyncio
    async def test_read_file_success(self, tmp_path):
        (tmp_path / "hello.txt").write_text("hello world", encoding="utf-8")
        config = _make_config(allow_read=True, base_dir=str(tmp_path))
        tool = ReadFileTool(config=config)
        result = await tool.execute(path="hello.txt")
        assert result.success
        assert result.data["content"] == "hello world"
        assert "hello.txt" in result.message

    @pytest.mark.asyncio
    async def test_read_file_workspace_relative_ok(self, tmp_path):
        """开启工作区隔离时，相对路径解析到当前 frontend/user 工作区。"""
        config = _make_workspace_sandbox_config(tmp_path, source="feishu")
        f = tmp_path / "workspace_parent" / "feishu" / "r1" / "note.md"
        f.parent.mkdir(parents=True)
        f.write_text("workspace", encoding="utf-8")
        tool = ReadFileTool(config=config)
        result = await tool.execute(
            path="note.md",
            __execution_context__=_ctx("feishu", "r1"),
        )
        assert result.success
        assert result.data["content"] == "workspace"

    @pytest.mark.asyncio
    async def test_read_file_workspace_allows_absolute_path_outside(self, tmp_path):
        """工作区外路径保持只读，read_file 仍可读取。"""
        config = _make_workspace_sandbox_config(tmp_path, source="feishu")
        external = tmp_path / "outside.txt"
        external.write_text("outside", encoding="utf-8")
        tool = ReadFileTool(config=config)
        result = await tool.execute(
            path=str(external),
            __execution_context__=_ctx("feishu", "r1"),
        )
        assert result.success
        assert result.data["content"] == "outside"

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_path):
        config = _make_config(allow_read=True, base_dir=str(tmp_path))
        tool = ReadFileTool(config=config)
        result = await tool.execute(path="nonexistent.txt")
        assert not result.success
        assert result.error == "FILE_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_read_file_permission_denied(self, tmp_path):
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        config = _make_config(allow_read=False, base_dir=str(tmp_path))
        tool = ReadFileTool(config=config)
        result = await tool.execute(path="f.txt")
        assert not result.success
        assert result.error == "PERMISSION_DENIED"
        assert "allow_read" in result.message

    @pytest.mark.asyncio
    async def test_read_file_allow_absolute_path(self, tmp_path):
        """path 支持任意有效路径（绝对路径可不限于 base_dir）"""
        external = tmp_path.parent / "external_read_test.txt"
        external.write_text("external content", encoding="utf-8")
        try:
            config = _make_config(allow_read=True, base_dir=str(tmp_path))
            tool = ReadFileTool(config=config)
            result = await tool.execute(path=str(external))
            assert result.success
            assert result.data["content"] == "external content"
        finally:
            external.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_read_file_missing_path(self):
        config = _make_config()
        tool = ReadFileTool(config=config)
        result = await tool.execute()
        assert not result.success
        assert result.error == "MISSING_PATH"

    @pytest.mark.asyncio
    async def test_read_file_with_max_lines(self, tmp_path):
        (tmp_path / "multi.txt").write_text(
            "line1\nline2\nline3\nline4\n", encoding="utf-8"
        )
        config = _make_config(allow_read=True, base_dir=str(tmp_path))
        tool = ReadFileTool(config=config)
        result = await tool.execute(path="multi.txt", max_lines=2)
        assert result.success
        assert result.data["content"] == "line1\nline2"
        # 元信息
        assert result.data["start_line"] == 1
        assert result.data["max_lines"] == 2
        assert result.data["total_lines"] == 4
        assert result.data["has_more"] is True

    @pytest.mark.asyncio
    async def test_read_file_with_start_line_and_max_lines(self, tmp_path):
        (tmp_path / "multi.txt").write_text("a\nb\nc\nd\n", encoding="utf-8")
        config = _make_config(allow_read=True, base_dir=str(tmp_path))
        tool = ReadFileTool(config=config)
        result = await tool.execute(path="multi.txt", start_line=2, max_lines=2)
        assert result.success
        assert result.data["content"] == "b\nc"
        assert result.data["start_line"] == 2
        assert result.data["max_lines"] == 2
        assert result.data["total_lines"] == 4
        assert result.data["has_more"] is True

    @pytest.mark.asyncio
    async def test_read_file_with_start_line_past_end_returns_empty(self, tmp_path):
        (tmp_path / "multi.txt").write_text("x\ny\n", encoding="utf-8")
        config = _make_config(allow_read=True, base_dir=str(tmp_path))
        tool = ReadFileTool(config=config)
        result = await tool.execute(path="multi.txt", start_line=10)
        assert result.success
        assert result.data["content"] == ""
        assert result.data["start_line"] == 10
        assert result.data["total_lines"] == 2
        assert result.data["has_more"] is False

    @pytest.mark.asyncio
    async def test_read_file_invalid_start_line(self, tmp_path):
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        config = _make_config(allow_read=True, base_dir=str(tmp_path))
        tool = ReadFileTool(config=config)
        result = await tool.execute(path="f.txt", start_line=0)
        assert not result.success
        assert result.error == "INVALID_START_LINE"

    @pytest.mark.asyncio
    async def test_read_file_invalid_max_lines(self, tmp_path):
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        config = _make_config(allow_read=True, base_dir=str(tmp_path))
        tool = ReadFileTool(config=config)
        result = await tool.execute(path="f.txt", max_lines=0)
        assert not result.success
        assert result.error == "INVALID_MAX_LINES"


# ============================================================================
# WriteFileTool
# ============================================================================


class TestWriteFileTool:
    def test_get_definition(self):
        config = _make_config()
        tool = WriteFileTool(config=config)
        defn = tool.get_definition()
        assert defn.name == "write_file"
        assert "path" in [p.name for p in defn.parameters]
        assert "content" in [p.name for p in defn.parameters]

    @pytest.mark.asyncio
    async def test_write_file_success(self, tmp_path):
        config = _make_config(allow_write=True, base_dir=str(tmp_path))
        tool = WriteFileTool(config=config)
        result = await tool.execute(path="new.txt", content="new content")
        assert result.success
        assert (tmp_path / "new.txt").read_text() == "new content"

    @pytest.mark.asyncio
    async def test_write_file_overwrite(self, tmp_path):
        (tmp_path / "existing.txt").write_text("old", encoding="utf-8")
        config = _make_config(allow_write=True, base_dir=str(tmp_path))
        tool = WriteFileTool(config=config)
        result = await tool.execute(path="existing.txt", content="new")
        assert result.success
        assert (tmp_path / "existing.txt").read_text() == "new"

    @pytest.mark.asyncio
    async def test_write_file_permission_denied(self, tmp_path):
        config = _make_config(allow_write=False, base_dir=str(tmp_path))
        tool = WriteFileTool(config=config)
        result = await tool.execute(path="x.txt", content="x")
        assert not result.success
        assert result.error == "PERMISSION_DENIED"
        assert "allow_write" in result.message

    @pytest.mark.asyncio
    async def test_write_file_permission_provider_denied(self, tmp_path):
        provider = AsyncMock(return_value=False)
        config = _make_config(allow_write=True, base_dir=str(tmp_path))
        tool = WriteFileTool(config=config, permission_provider=provider)
        result = await tool.execute(path="x.txt", content="x")
        assert not result.success
        assert result.error == "USER_DENIED"
        provider.assert_called_once()
        call_args = provider.call_args[0]
        assert call_args[0] == "write"
        assert "x.txt" in call_args[1]

    @pytest.mark.asyncio
    async def test_write_file_permission_provider_allowed(self, tmp_path):
        provider = AsyncMock(return_value=True)
        config = _make_config(allow_write=True, base_dir=str(tmp_path))
        tool = WriteFileTool(config=config, permission_provider=provider)
        result = await tool.execute(path="ok.txt", content="ok")
        assert result.success
        assert (tmp_path / "ok.txt").read_text() == "ok"
        provider.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_file_sub_mode_denied(self, tmp_path):
        """sub 模式下统一禁止 write_file。"""
        config = _make_config(allow_write=True, base_dir=str(tmp_path))
        tool = WriteFileTool(config=config)
        result = await tool.execute(
            path="x.txt",
            content="x",
            __execution_context__={"tool_mode": "sub", "source": "cli"},
        )
        assert not result.success
        assert result.error == "PERMISSION_DENIED"
        assert "sub 模式" in result.message
        assert not (tmp_path / "x.txt").exists()

    @pytest.mark.asyncio
    async def test_write_file_workspace_ok(self, tmp_path):
        """开启工作区隔离时，可写入当前 frontend/user 工作区。"""
        config = _make_workspace_sandbox_config(tmp_path, source="feishu", allow_write=True)
        tool = WriteFileTool(config=config)
        result = await tool.execute(
            path="hello.py",
            content="print(1)",
            __execution_context__=_ctx("feishu", "wbzd"),
        )
        assert result.success
        expected = tmp_path / "workspace_parent" / "feishu" / "wbzd" / "hello.py"
        assert expected.read_text() == "print(1)"

    @pytest.mark.asyncio
    async def test_write_file_forbidden_outside_workspace(self, tmp_path):
        """工作区外路径对 write_file 保持只读。"""
        config = _make_workspace_sandbox_config(tmp_path, source="feishu", allow_write=True)
        tool = WriteFileTool(config=config)
        outside = tmp_path / "evil.txt"
        result = await tool.execute(
            path=str(outside),
            content="x",
            __execution_context__=_ctx("feishu", "wbzd"),
        )
        assert not result.success
        assert result.error == "FORBIDDEN_PATH"
        assert not outside.exists()

    @pytest.mark.asyncio
    async def test_write_file_tmp_dir_allowed(self, tmp_path):
        """允许写入 frontend/user 专属临时目录。"""
        config = _make_workspace_sandbox_config(tmp_path, source="feishu", allow_write=True)
        tool = WriteFileTool(config=config)
        temp_file = "/tmp/macchiato/feishu/wbzd/script.py"
        result = await tool.execute(
            path=temp_file,
            content="print(1)",
            __execution_context__=_ctx("feishu", "wbzd"),
        )
        try:
            assert result.success
            assert Path(temp_file).read_text() == "print(1)"
        finally:
            Path(temp_file).unlink(missing_ok=True)


# ============================================================================
# ModifyFileTool
# ============================================================================


class TestModifyFileTool:
    def test_get_definition(self):
        config = _make_config()
        tool = ModifyFileTool(config=config)
        defn = tool.get_definition()
        assert defn.name == "modify_file"
        param_names = [p.name for p in defn.parameters]
        assert "mode" in param_names
        assert "old_text" in param_names
        assert "new_text" in param_names
        assert "content" in param_names

    @pytest.mark.asyncio
    async def test_modify_file_sub_mode_denied(self, tmp_path):
        """sub 模式下统一禁止 modify_file。"""
        (tmp_path / "f.txt").write_text("old", encoding="utf-8")
        config = _make_config(allow_modify=True, base_dir=str(tmp_path))
        tool = ModifyFileTool(config=config)
        result = await tool.execute(
            path="f.txt",
            mode="search_replace",
            old_text="old",
            new_text="new",
            __execution_context__={"tool_mode": "sub", "source": "cli"},
        )
        assert not result.success
        assert result.error == "PERMISSION_DENIED"
        assert "sub 模式" in result.message
        assert (tmp_path / "f.txt").read_text() == "old"

    @pytest.mark.asyncio
    async def test_modify_file_workspace_ok(self, tmp_path):
        """开启工作区隔离时，可修改当前 frontend/user 工作区内文件。"""
        config = _make_workspace_sandbox_config(tmp_path, source="feishu", allow_modify=True)
        ws_file = tmp_path / "workspace_parent" / "feishu" / "u2" / "app.py"
        ws_file.parent.mkdir(parents=True)
        ws_file.write_text("old", encoding="utf-8")
        tool = ModifyFileTool(config=config)
        result = await tool.execute(
            path="app.py",
            mode="search_replace",
            old_text="old",
            new_text="new",
            __execution_context__=_ctx("feishu", "u2"),
        )
        assert result.success
        assert ws_file.read_text() == "new"

    @pytest.mark.asyncio
    async def test_modify_file_forbidden_outside_workspace(self, tmp_path):
        """工作区外路径对 modify_file 保持只读。"""
        config = _make_workspace_sandbox_config(tmp_path, source="feishu", allow_modify=True)
        outside = tmp_path / "outside.py"
        outside.write_text("old", encoding="utf-8")
        tool = ModifyFileTool(config=config)
        result = await tool.execute(
            path=str(outside),
            mode="search_replace",
            old_text="old",
            new_text="new",
            __execution_context__=_ctx("feishu", "u2"),
        )
        assert not result.success
        assert result.error == "FORBIDDEN_PATH"
        assert outside.read_text() == "old"

    @pytest.mark.asyncio
    async def test_modify_file_tmp_dir_allowed(self, tmp_path):
        """允许修改 frontend/user 专属临时目录内文件。"""
        config = _make_workspace_sandbox_config(tmp_path, source="feishu", allow_modify=True)
        temp_file = Path("/tmp/macchiato/feishu/u2/app.py")
        temp_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file.write_text("old", encoding="utf-8")
        tool = ModifyFileTool(config=config)
        result = await tool.execute(
            path=str(temp_file),
            mode="search_replace",
            old_text="old",
            new_text="new",
            __execution_context__=_ctx("feishu", "u2"),
        )
        try:
            assert result.success
            assert temp_file.read_text() == "new"
        finally:
            temp_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_modify_file_search_replace_exact_success(self, tmp_path):
        (tmp_path / "app.py").write_text("def foo():\n    pass\n", encoding="utf-8")
        config = _make_config(allow_modify=True, base_dir=str(tmp_path))
        tool = ModifyFileTool(config=config)
        result = await tool.execute(
            path="app.py",
            mode="search_replace",
            old_text="def foo():\n    pass",
            new_text="def foo():\n    return 1",
        )
        assert result.success
        assert (tmp_path / "app.py").read_text() == "def foo():\n    return 1\n"

    @pytest.mark.asyncio
    async def test_modify_file_search_replace_line_trimmed_fallback(self, tmp_path):
        (tmp_path / "f.py").write_text("def bar():  \n    x = 1  \n", encoding="utf-8")
        config = _make_config(allow_modify=True, base_dir=str(tmp_path))
        tool = ModifyFileTool(config=config)
        result = await tool.execute(
            path="f.py",
            mode="search_replace",
            old_text="def bar():\n    x = 1",
            new_text="def bar():\n    x = 2",
        )
        assert result.success
        assert "x = 2" in (tmp_path / "f.py").read_text()

    @pytest.mark.asyncio
    async def test_modify_file_search_replace_failure_suggests_fallback(self, tmp_path):
        (tmp_path / "f.txt").write_text("actual content", encoding="utf-8")
        config = _make_config(allow_modify=True, base_dir=str(tmp_path))
        tool = ModifyFileTool(config=config)
        result = await tool.execute(
            path="f.txt",
            mode="search_replace",
            old_text="nonexistent text",
            new_text="replacement",
        )
        assert not result.success
        assert result.error == "SEARCH_REPLACE_FAILED"
        assert "read_file" in result.message or "write_file" in result.message

    @pytest.mark.asyncio
    async def test_modify_file_append_success(self, tmp_path):
        (tmp_path / "log.txt").write_text("line1\n", encoding="utf-8")
        config = _make_config(allow_modify=True, base_dir=str(tmp_path))
        tool = ModifyFileTool(config=config)
        result = await tool.execute(path="log.txt", content="line2\n", mode="append")
        assert result.success
        assert (tmp_path / "log.txt").read_text() == "line1\nline2\n"

    @pytest.mark.asyncio
    async def test_modify_file_overwrite_success(self, tmp_path):
        (tmp_path / "f.txt").write_text("old", encoding="utf-8")
        config = _make_config(allow_modify=True, base_dir=str(tmp_path))
        tool = ModifyFileTool(config=config)
        result = await tool.execute(path="f.txt", content="new", mode="overwrite")
        assert result.success
        assert (tmp_path / "f.txt").read_text() == "new"

    @pytest.mark.asyncio
    async def test_modify_file_permission_denied(self, tmp_path):
        config = _make_config(allow_modify=False, base_dir=str(tmp_path))
        tool = ModifyFileTool(config=config)
        result = await tool.execute(path="x.txt", content="x", mode="append")
        assert not result.success
        assert result.error == "PERMISSION_DENIED"
        assert "allow_modify" in result.message

    @pytest.mark.asyncio
    async def test_modify_file_invalid_mode(self, tmp_path):
        config = _make_config(allow_modify=True, base_dir=str(tmp_path))
        tool = ModifyFileTool(config=config)
        result = await tool.execute(path="x.txt", content="x", mode="invalid")
        assert not result.success
        assert result.error == "INVALID_MODE"

    @pytest.mark.asyncio
    async def test_modify_file_search_replace_missing_params(self, tmp_path):
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        config = _make_config(allow_modify=True, base_dir=str(tmp_path))
        tool = ModifyFileTool(config=config)
        result = await tool.execute(path="f.txt", mode="search_replace", old_text="x")
        assert not result.success
        assert result.error == "MISSING_PARAMS"


# ============================================================================
# 集成
# ============================================================================


class TestFileToolsIntegration:
    @pytest.mark.asyncio
    async def test_read_after_write(self, tmp_path):
        config = _make_config(allow_read=True, allow_write=True, base_dir=str(tmp_path))
        w = WriteFileTool(config=config)
        r = ReadFileTool(config=config)
        await w.execute(path="test.txt", content="hello")
        result = await r.execute(path="test.txt")
        assert result.success
        assert result.data["content"] == "hello"
