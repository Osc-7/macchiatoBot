from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.content import ContentReference
from frontend.feishu.content_resolver import FeishuContentResolver


class _FakeFeishuClient:
    def __init__(self, payload: bytes, mime: str, filename: str) -> None:
        self._payload = payload
        self._mime = mime
        self._filename = filename

    async def download_message_resource(self, **kwargs):
        return self._payload, self._mime, self._filename


@pytest.mark.asyncio
async def test_resolve_feishu_document_saved_and_inlined(monkeypatch, tmp_path: Path):
    resolver = FeishuContentResolver(
        client=_FakeFeishuClient(
            payload=b"hello from feishu document",
            mime="text/plain",
            filename="note.txt",
        )
    )

    monkeypatch.setattr(
        FeishuContentResolver,
        "_workspace_upload_dir",
        staticmethod(lambda source, user_id: tmp_path),
    )

    ref = ContentReference(
        source="feishu",
        ref_type="document",
        key="file_xxx",
        extra={"message_id": "om_1", "source": "feishu", "user_id": "u1"},
    )
    item = await resolver.resolve(ref)
    assert item is not None
    assert item["type"] == "text"
    assert "note.txt" in item["text"]
    assert "hello from feishu document" in item["text"]
    assert (tmp_path / "note.txt").exists()


@pytest.mark.asyncio
async def test_resolve_feishu_pdf_as_generic_user_file(monkeypatch, tmp_path: Path):
    resolver = FeishuContentResolver(
        client=_FakeFeishuClient(
            payload=b"%PDF-1.7 fake",
            mime="application/pdf",
            filename="spec.pdf",
        )
    )

    monkeypatch.setattr(
        FeishuContentResolver,
        "_workspace_upload_dir",
        staticmethod(lambda source, user_id: tmp_path),
    )

    ref = ContentReference(
        source="feishu",
        ref_type="document",
        key="file_pdf",
        extra={"message_id": "om_pdf", "source": "feishu", "user_id": "u1"},
    )
    item = await resolver.resolve(ref)
    assert item is not None
    assert item["type"] == "user_file"
    assert item["mime_type"] == "application/pdf"
    assert item["name"] == "spec.pdf"
    assert item["path"].endswith("spec.pdf")
    assert item["file_data"]
    assert (tmp_path / "spec.pdf").exists()


@pytest.mark.asyncio
async def test_resolve_feishu_pdf_keeps_unicode_filename_from_message_metadata(
    monkeypatch, tmp_path: Path
):
    resolver = FeishuContentResolver(
        client=_FakeFeishuClient(
            payload=b"%PDF-1.7 fake",
            mime="application/octet-stream",
            filename="attachment.bin",
        )
    )

    monkeypatch.setattr(
        FeishuContentResolver,
        "_workspace_upload_dir",
        staticmethod(lambda source, user_id: tmp_path),
    )

    ref = ContentReference(
        source="feishu",
        ref_type="document",
        key="file_pdf",
        extra={
            "message_id": "om_pdf",
            "source": "feishu",
            "user_id": "u1",
            "file_name": "测试文档.pdf",
        },
    )
    item = await resolver.resolve(ref)
    assert item is not None
    assert item["type"] == "user_file"
    assert item["mime_type"] == "application/pdf"
    assert item["name"] == "测试文档.pdf"
    assert item["path"].endswith("测试文档.pdf")
    assert (tmp_path / "测试文档.pdf").exists()


@pytest.mark.asyncio
async def test_resolve_feishu_image_keeps_multimodal(monkeypatch, tmp_path: Path):
    resolver = FeishuContentResolver(
        client=_FakeFeishuClient(
            payload=b"\x89PNG\r\n\x1a\n",
            mime="image/png",
            filename="x.png",
        )
    )

    monkeypatch.setattr(
        FeishuContentResolver,
        "_workspace_upload_dir",
        staticmethod(lambda source, user_id: tmp_path),
    )

    ref = ContentReference(
        source="feishu",
        ref_type="image",
        key="img_xxx",
        extra={"message_id": "om_2", "source": "feishu", "user_id": "u1"},
    )
    item = await resolver.resolve(ref)
    assert item is not None
    assert item["type"] == "image_url"
    assert "data:image/png;base64," in item["image_url"]["url"]
    assert item["path"].endswith("x.png")
    assert (tmp_path / "x.png").exists()
