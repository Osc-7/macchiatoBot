"""
文件读写工具测试 - 测试 read_file, write_file, modify_file
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from schedule_agent.config import Config, LLMConfig, FileToolsConfig
from schedule_agent.core.tools.file_tools import ReadFileTool, WriteFileTool, ModifyFileTool
from schedule_agent.core.tools.base import ToolDefinition


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
    )


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
        assert "path" in [p.name for p in defn.parameters]

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
        result = await tool.execute(
            path="log.txt", content="line2\n", mode="append"
        )
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
        result = await tool.execute(
            path="x.txt", content="x", mode="invalid"
        )
        assert not result.success
        assert result.error == "INVALID_MODE"

    @pytest.mark.asyncio
    async def test_modify_file_search_replace_missing_params(self, tmp_path):
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        config = _make_config(allow_modify=True, base_dir=str(tmp_path))
        tool = ModifyFileTool(config=config)
        result = await tool.execute(
            path="f.txt", mode="search_replace", old_text="x"
        )
        assert not result.success
        assert result.error == "MISSING_PARAMS"


# ============================================================================
# 集成
# ============================================================================


class TestFileToolsIntegration:
    @pytest.mark.asyncio
    async def test_read_after_write(self, tmp_path):
        config = _make_config(
            allow_read=True, allow_write=True, base_dir=str(tmp_path)
        )
        w = WriteFileTool(config=config)
        r = ReadFileTool(config=config)
        await w.execute(path="test.txt", content="hello")
        result = await r.execute(path="test.txt")
        assert result.success
        assert result.data["content"] == "hello"
