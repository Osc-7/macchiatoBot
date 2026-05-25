"""
多模态媒体挂载工具与回复附图工具测试。
"""

import pytest

from agent_core.agent.media_helpers import collect_outgoing_attachment
from agent_core.config import CommandToolsConfig, Config, FileToolsConfig, LLMConfig
from agent_core.remote.workspace_state import (
    activate_remote_workspace,
    clear_remote_workspace_state,
)
from agent_core.tools.base import ToolResult
from system.tools.media_tools import (
    AttachFileToReplyTool,
    AttachImageToReplyTool,
    AttachMediaTool,
)


def _workspace_config(tmp_path):
    return Config(
        llm=LLMConfig(api_key="k", model="m"),
        file_tools=FileToolsConfig(base_dir=str(tmp_path)),
        command_tools=CommandToolsConfig(
            base_dir=str(tmp_path),
            workspace_base_dir=str(tmp_path / "workspace_parent"),
            workspace_isolation_enabled=True,
        ),
    )


def test_collect_outgoing_attachment_supports_inline_base64_file():
    result = ToolResult(
        success=True,
        data=None,
        message="ok",
        metadata={
            "outgoing_attachment": {
                "type": "file",
                "content_base64": "b2s=",
                "file_name": "report.txt",
                "mime_type": "text/plain",
            }
        },
    )
    attachments = []

    collect_outgoing_attachment(result, attachments)

    assert attachments == [
        {
            "type": "file",
            "content_base64": "b2s=",
            "file_name": "report.txt",
            "mime_type": "text/plain",
        }
    ]


class TestAttachMediaTool:
    @pytest.mark.asyncio
    async def test_execute_requires_path_or_paths(self):
        tool = AttachMediaTool()
        result = await tool.execute()
        assert result.success is False
        assert result.error == "MISSING_MEDIA_PATH"

    @pytest.mark.asyncio
    async def test_execute_with_single_path(self, tmp_path):
        img = tmp_path / "page_1.png"
        img.write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
            )
        )
        tool = AttachMediaTool(config=_workspace_config(tmp_path))
        result = await tool.execute(path=str(img))
        assert result.success is True
        assert result.metadata.get("embed_in_next_call") is True
        assert result.metadata.get("media_items")
        assert result.metadata["media_items"][0]["type"] == "media_ref"
        assert result.data["paths"] == [str(img)]

    @pytest.mark.asyncio
    async def test_execute_with_paths_list_merges_and_deduplicates(self, tmp_path):
        a = tmp_path / "a.png"
        b = tmp_path / "b.png"
        payload = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
        )
        a.write_bytes(payload)
        b.write_bytes(payload)
        tool = AttachMediaTool(config=_workspace_config(tmp_path))
        result = await tool.execute(path=str(a), paths=[str(a), str(b)])
        assert result.success is True
        assert result.data["paths"] == [str(a), str(b)]
        assert len(result.metadata["media_items"]) == 2


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
    async def test_execute_with_workspace_relative_path(self, tmp_path):
        cfg = _workspace_config(tmp_path)
        img = tmp_path / "workspace_parent" / "feishu" / "u1" / "pic.png"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        tool = AttachImageToReplyTool(config=cfg)

        result = await tool.execute(
            image_path="pic.png",
            __execution_context__={"source": "feishu", "user_id": "u1"},
        )

        assert result.success is True
        assert result.data["path"] == str(img.resolve())

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

    @pytest.mark.asyncio
    async def test_execute_with_remote_workspace_image_path_succeeds(
        self, tmp_path, monkeypatch
    ):
        cfg = _workspace_config(tmp_path)
        tool = AttachImageToReplyTool(config=cfg)
        clear_remote_workspace_state()
        try:
            activate_remote_workspace(
                session_id="feishu:u1",
                login="local-dev",
                requested_path="~/proj",
                resolved_path=str(tmp_path / "remote-proj"),
            )
            import agent_core.remote.worker_registry as registry_mod

            class _FakeRegistry:
                async def file_blob_read(self, **kwargs):
                    class _R:
                        error = None
                        content_base64 = "iVBORw0KGgo="
                        file_name = "pic.png"
                        mime_type = "image/png"
                        bytes_read = 8
                        truncated = False

                    return _R()

            monkeypatch.setattr(
                registry_mod, "get_remote_worker_registry", lambda: _FakeRegistry()
            )
            result = await tool.execute(
                image_path="pic.png",
                __execution_context__={
                    "source": "feishu",
                    "user_id": "u1",
                    "session_id": "feishu:u1",
                },
            )
        finally:
            clear_remote_workspace_state()

        assert result.success is True
        assert result.metadata.get("outgoing_attachment") == {
            "type": "image",
            "content_base64": "iVBORw0KGgo=",
            "content_type": "image/png",
            "file_name": "pic.png",
        }

    @pytest.mark.asyncio
    async def test_execute_with_remote_workspace_image_blob_timeout_returns_remote_error(
        self, tmp_path, monkeypatch
    ):
        cfg = _workspace_config(tmp_path)
        tool = AttachImageToReplyTool(config=cfg)
        clear_remote_workspace_state()
        try:
            activate_remote_workspace(
                session_id="feishu:u1",
                login="local-dev",
                requested_path="~/proj",
                resolved_path=str(tmp_path / "remote-proj"),
            )
            import agent_core.remote.worker_registry as registry_mod

            class _FakeRegistry:
                async def file_blob_read(self, **kwargs):
                    raise TimeoutError()

            monkeypatch.setattr(
                registry_mod, "get_remote_worker_registry", lambda: _FakeRegistry()
            )
            result = await tool.execute(
                image_path="pic.png",
                __execution_context__={
                    "source": "feishu",
                    "user_id": "u1",
                    "session_id": "feishu:u1",
                },
            )
        finally:
            clear_remote_workspace_state()

        assert result.success is False
        assert result.error == "REMOTE_ATTACHMENT_READ_FAILED"
        assert "超时" in result.message


class TestAttachFileToReplyTool:
    @pytest.mark.asyncio
    async def test_execute_requires_file_path_or_file_url(self):
        tool = AttachFileToReplyTool()
        result = await tool.execute()
        assert result.success is False
        assert result.error == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_execute_with_valid_path_returns_outgoing_attachment(self, tmp_path):
        p = tmp_path / "report.txt"
        p.write_text("ok", encoding="utf-8")
        tool = AttachFileToReplyTool()
        result = await tool.execute(file_path=str(p))
        assert result.success is True
        assert result.metadata.get("outgoing_attachment") == {
            "type": "file",
            "path": str(p.resolve()),
        }

    @pytest.mark.asyncio
    async def test_execute_with_workspace_relative_path(self, tmp_path):
        cfg = _workspace_config(tmp_path)
        p = tmp_path / "workspace_parent" / "feishu" / "u1" / "report.txt"
        p.parent.mkdir(parents=True)
        p.write_text("ok", encoding="utf-8")
        tool = AttachFileToReplyTool(config=cfg)

        result = await tool.execute(
            file_path="report.txt",
            __execution_context__={"source": "feishu", "user_id": "u1"},
        )

        assert result.success is True
        assert result.data["path"] == str(p.resolve())

    @pytest.mark.asyncio
    async def test_execute_with_remote_workspace_path_reads_blob_and_succeeds(
        self, tmp_path, monkeypatch
    ):
        cfg = _workspace_config(tmp_path)
        tool = AttachFileToReplyTool(config=cfg)
        clear_remote_workspace_state()
        try:
            activate_remote_workspace(
                session_id="feishu:u1",
                login="local-dev",
                requested_path="~/proj",
                resolved_path=str(tmp_path / "remote-proj"),
            )
            import agent_core.remote.worker_registry as registry_mod

            class _FakeRegistry:
                async def file_blob_read(self, **kwargs):
                    class _R:
                        error = None
                        content_base64 = "b2s="
                        file_name = "report.txt"
                        mime_type = "text/plain"
                        bytes_read = 2
                        truncated = False

                    return _R()

            monkeypatch.setattr(
                registry_mod, "get_remote_worker_registry", lambda: _FakeRegistry()
            )
            result = await tool.execute(
                file_path="report.txt",
                __execution_context__={
                    "source": "feishu",
                    "user_id": "u1",
                    "session_id": "feishu:u1",
                },
            )
        finally:
            clear_remote_workspace_state()

        assert result.success is True
        assert result.metadata.get("outgoing_attachment") == {
            "type": "file",
            "content_base64": "b2s=",
            "mime_type": "text/plain",
            "file_name": "report.txt",
        }

    @pytest.mark.asyncio
    async def test_execute_with_remote_workspace_path_falls_back_to_text_read(
        self, tmp_path, monkeypatch
    ):
        cfg = _workspace_config(tmp_path)
        tool = AttachFileToReplyTool(config=cfg)
        clear_remote_workspace_state()
        try:
            activate_remote_workspace(
                session_id="feishu:u1",
                login="local-dev",
                requested_path="~/proj",
                resolved_path=str(tmp_path / "remote-proj"),
            )
            import agent_core.remote.worker_registry as registry_mod

            class _FakeRegistry:
                async def file_blob_read(self, **kwargs):
                    raise TimeoutError()

                async def file_read(self, **kwargs):
                    class _R:
                        error = None
                        content = "report content"
                        truncated = False

                    return _R()

            monkeypatch.setattr(
                registry_mod, "get_remote_worker_registry", lambda: _FakeRegistry()
            )
            result = await tool.execute(
                file_path="report.txt",
                __execution_context__={
                    "source": "feishu",
                    "user_id": "u1",
                    "session_id": "feishu:u1",
                },
            )
        finally:
            clear_remote_workspace_state()

        assert result.success is True
        assert result.metadata.get("outgoing_attachment") == {
            "type": "file",
            "content_base64": "cmVwb3J0IGNvbnRlbnQ=",
            "mime_type": "text/plain; charset=utf-8",
            "file_name": "report.txt",
        }

    @pytest.mark.asyncio
    async def test_execute_with_url_returns_outgoing_attachment(self):
        tool = AttachFileToReplyTool()
        result = await tool.execute(
            file_url="https://example.com/spec.pdf", file_name="spec.pdf"
        )
        assert result.success is True
        assert result.metadata.get("outgoing_attachment") == {
            "type": "file",
            "url": "https://example.com/spec.pdf",
            "file_name": "spec.pdf",
        }
