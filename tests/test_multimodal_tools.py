"""
多模态媒体挂载工具与回复附图工具测试。
"""

import pytest

from system.tools.media_tools import AttachMediaTool, AttachImageToReplyTool


class TestAttachMediaTool:
    @pytest.mark.asyncio
    async def test_execute_requires_path_or_paths(self):
        tool = AttachMediaTool()
        result = await tool.execute()
        assert result.success is False
        assert result.error == "MISSING_MEDIA_PATH"

    @pytest.mark.asyncio
    async def test_execute_with_single_path(self):
        tool = AttachMediaTool()
        result = await tool.execute(path="user_file/page_1.png")
        assert result.success is True
        assert result.metadata.get("embed_in_next_call") is True
        assert "paths" in result.data
        assert result.data["paths"] == ["user_file/page_1.png"]

    @pytest.mark.asyncio
    async def test_execute_with_paths_list_merges_and_deduplicates(self):
        tool = AttachMediaTool()
        result = await tool.execute(
            path="user_file/a.png",
            paths=["user_file/a.png", "user_file/b.png"],
        )
        assert result.success is True
        assert result.data["paths"] == ["user_file/a.png", "user_file/b.png"]


class TestAttachImageToReplyTool:
    @pytest.mark.asyncio
    async def test_execute_requires_image_path_or_image_url(self):
        tool = AttachImageToReplyTool()
        result = await tool.execute()
        assert result.success is False
        assert result.error == "INVALID_INPUT"
        result_both = await tool.execute(
            image_path="/tmp/x.png", image_url="https://example.com/x.png"
        )
        assert result_both.success is False

    @pytest.mark.asyncio
    async def test_execute_with_nonexistent_path_fails(self):
        tool = AttachImageToReplyTool()
        result = await tool.execute(image_path="/nonexistent/image_xyz_12345.png")
        assert result.success is False
        assert result.error == "FILE_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_execute_with_valid_path_returns_outgoing_attachment(self, tmp_path):
        (tmp_path / "test.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        tool = AttachImageToReplyTool()
        result = await tool.execute(image_path=str(tmp_path / "test.png"))
        assert result.success is True
        assert result.metadata.get("outgoing_attachment") == {
            "type": "image",
            "path": str((tmp_path / "test.png").resolve()),
        }
        assert "path" in result.data and result.data["type"] == "image"

    @pytest.mark.asyncio
    async def test_execute_with_url_returns_outgoing_attachment(self):
        tool = AttachImageToReplyTool()
        result = await tool.execute(image_url="https://example.com/diagram.png")
        assert result.success is True
        assert result.metadata.get("outgoing_attachment") == {
            "type": "image",
            "url": "https://example.com/diagram.png",
        }

    @pytest.mark.asyncio
    async def test_execute_with_invalid_url_fails(self):
        tool = AttachImageToReplyTool()
        result = await tool.execute(image_url="not-a-url")
        assert result.success is False
        assert result.error == "INVALID_URL"
