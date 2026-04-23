from __future__ import annotations

import httpx
import pytest

from frontend.feishu.client import FeishuClient


@pytest.mark.asyncio
async def test_upload_file_includes_file_name_and_pdf_type(monkeypatch):
    captured: dict = {}

    async def _fake_get_token(self) -> str:
        return "test-token"

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200, json={"code": 0, "data": {"file_key": "file_123"}})

    transport = httpx.MockTransport(_handler)

    class _FakeAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(FeishuClient, "_get_tenant_access_token", _fake_get_token)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    client = FeishuClient(timeout_seconds=5.0)
    file_key = await client.upload_file(
        file_bytes=b"%PDF-1.7 fake",
        file_name="test_file.pdf",
    )

    assert file_key == "file_123"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/open-apis/im/v1/files")
    assert captured["headers"]["authorization"] == "Bearer test-token"
    body = captured["body"].decode("utf-8", errors="ignore")
    assert 'name="file_type"' in body
    assert "\r\npdf\r\n" in body
    assert 'name="file_name"' in body
    assert "\r\ntest_file.pdf\r\n" in body
    assert 'name="file"; filename="test_file.pdf"' in body


def test_infer_upload_file_type():
    assert FeishuClient._infer_upload_file_type("a.pdf") == "pdf"
    assert FeishuClient._infer_upload_file_type("a.docx") == "doc"
    assert FeishuClient._infer_upload_file_type("a.xlsx") == "xls"
    assert FeishuClient._infer_upload_file_type("a.pptx") == "ppt"
    assert FeishuClient._infer_upload_file_type("a.zip") == "stream"
