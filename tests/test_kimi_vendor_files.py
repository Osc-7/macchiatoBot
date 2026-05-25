"""Kimi vendor Files API 集成测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_core.agent.media_helpers import (
    hydrate_messages_for_api,
    persist_kimi_ms_urls_in_context,
)
from agent_core.llm.vendor_files.kimi import (
    KimiVendorFilesClient,
    build_kimi_vendor_files_client,
    ms_url,
    resolve_kimi_files_base_url,
)


def test_resolve_kimi_files_base_url():
    assert (
        resolve_kimi_files_base_url("https://api.kimi.com/coding/v1")
        == "https://api.kimi.com/coding/v1"
    )
    assert (
        resolve_kimi_files_base_url("https://api.moonshot.cn/v1")
        == "https://api.moonshot.cn/v1"
    )


def test_kimi_client_uses_cache(tmp_path: Path):
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")

    client = KimiVendorFilesClient(
        files_base_url="https://api.kimi.com/coding/v1",
        api_key="k",
        cache={},
    )

    with patch.object(client, "_upload", return_value="file_abc") as upload:
        first = client.ensure_ms_url(path=str(png), media_type="image")
        second = client.ensure_ms_url(path=str(png), media_type="image")

    assert first == ms_url("file_abc")
    assert second == ms_url("file_abc")
    upload.assert_called_once()


def test_hydrate_prefers_ms_url_when_kimi_client_available(tmp_path: Path):
    png = tmp_path / "chart.png"
    png.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
        )
    )
    kimi = MagicMock(spec=KimiVendorFilesClient)
    kimi.ensure_ms_url.return_value = "ms://file_xyz"

    stored = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {
                    "type": "media_ref",
                    "media_type": "image",
                    "path": str(png),
                    "name": "chart.png",
                },
            ],
        }
    ]

    hydrated = hydrate_messages_for_api(
        stored,
        current_turn_id=1,
        vision_supported=True,
        kimi_files=kimi,
    )
    parts = hydrated[0]["content"]
    img = next(p for p in parts if p.get("type") == "image_url")
    assert img["image_url"]["url"] == "ms://file_xyz"
    assert "base64" not in img["image_url"]["url"]


def test_persist_ms_url_writes_back_to_context(tmp_path: Path):
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    kimi = MagicMock(spec=KimiVendorFilesClient)
    kimi.ensure_ms_url.return_value = "ms://cached_id"

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "media_ref",
                    "media_type": "image",
                    "path": str(png),
                }
            ],
        }
    ]
    persist_kimi_ms_urls_in_context(messages, kimi_files=kimi)
    assert messages[0]["content"][0]["url"] == "ms://cached_id"
    assert messages[0]["content"][0]["vendor"] == "kimi"


def test_build_client_from_provider_base_url():
    client = build_kimi_vendor_files_client(
        provider_base_url="https://api.kimi.com/coding/v1",
        api_key="secret",
    )
    assert client is not None
    assert client.files_base_url.endswith("/v1")
