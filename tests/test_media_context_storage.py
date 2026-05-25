"""多模态内容：上下文仅存引用，API 请求时按 turn 临时注入二进制。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.agent.media_helpers import (
    adapt_content_items_for_provider,
    hydrate_messages_for_api,
    normalize_media_items_for_context,
)
from agent_core.context.conversation import ConversationContext


def test_normalize_media_items_strips_base64_from_image_url():
    items = normalize_media_items_for_context(
        [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
                "path": "/tmp/a.png",
                "name": "a.png",
            }
        ]
    )
    assert len(items) == 1
    assert items[0]["type"] == "media_ref"
    assert items[0]["media_type"] == "image"
    assert items[0]["path"] == "/tmp/a.png"
    assert "url" not in items[0] or not items[0]["url"].startswith("data:")


def test_context_add_user_message_never_stores_file_data():
    ctx = ConversationContext()
    ctx.add_user_message(
        "请看文件",
        media_items=[
            {
                "type": "user_file",
                "path": "/tmp/spec.pdf",
                "name": "spec.pdf",
                "mime_type": "application/pdf",
                "file_data": "JVBERi0xLjc=",
            }
        ],
        turn_id=3,
    )
    msg = ctx.messages[-1]
    assert msg["_turn_id"] == 3
    assert isinstance(msg["content"], list)
    assert len(msg["content"]) == 1
    assert msg["content"][0]["type"] == "text"
    assert "file_data" not in str(msg)


def test_hydrate_messages_never_injects_file_blocks(tmp_path: Path):
    pdf = tmp_path / "spec.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")

    stored = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "old"},
                {
                    "type": "media_ref",
                    "media_type": "file",
                    "path": str(pdf),
                    "name": "spec.pdf",
                    "mime_type": "application/pdf",
                },
            ],
            "_turn_id": 1,
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "new"},
                {
                    "type": "media_ref",
                    "media_type": "file",
                    "path": str(pdf),
                    "name": "spec.pdf",
                    "mime_type": "application/pdf",
                },
            ],
            "_turn_id": 2,
        },
    ]

    hydrated = hydrate_messages_for_api(
        stored,
        current_turn_id=2,
        vision_supported=True,
        enable_native_file_blocks=True,
        supported_file_mime_types=["application/pdf"],
    )

    for msg in hydrated:
        parts = msg["content"]
        assert all(p.get("type") != "file" for p in parts if isinstance(p, dict))
        assert all(p.get("type") != "media_ref" for p in parts if isinstance(p, dict))


def test_hydrate_images_for_all_user_turns(tmp_path: Path):
    png = tmp_path / "chart.png"
    png.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
        )
    )

    stored = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "turn1"},
                {
                    "type": "media_ref",
                    "media_type": "image",
                    "path": str(png),
                    "name": "chart.png",
                    "mime_type": "image/png",
                },
            ],
            "_turn_id": 1,
        },
        {"role": "assistant", "content": "ok"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "turn2 follow up"}],
            "_turn_id": 2,
        },
    ]

    hydrated = hydrate_messages_for_api(
        stored,
        current_turn_id=2,
        vision_supported=True,
    )

    turn1_parts = hydrated[0]["content"]
    assert any(p.get("type") == "image_url" for p in turn1_parts if isinstance(p, dict))
    assert all(p.get("type") != "media_ref" for p in turn1_parts if isinstance(p, dict))
    turn2_parts = hydrated[2]["content"]
    assert turn2_parts == [{"type": "text", "text": "turn2 follow up"}]


def test_hydrate_messages_strips_legacy_file_blocks_from_old_turns(tmp_path: Path):
    pdf = tmp_path / "spec.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")

    stored = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "old pdf"},
                {
                    "type": "file",
                    "file": {
                        "filename": "spec.pdf",
                        "file_data": "A" * 10000,
                    },
                    "mime_type": "application/pdf",
                    "path": str(pdf),
                    "name": "spec.pdf",
                },
            ],
            "_turn_id": 1,
        },
    ]

    hydrated = hydrate_messages_for_api(
        stored,
        current_turn_id=2,
        vision_supported=True,
        enable_native_file_blocks=True,
        supported_file_mime_types=["application/pdf"],
    )

    parts = hydrated[0]["content"]
    assert all(
        not (
            isinstance(p, dict)
            and p.get("type") == "file"
            and isinstance(p.get("file"), dict)
            and p["file"].get("file_data")
        )
        for p in parts
    )
    assert all(p.get("type") != "media_ref" for p in parts if isinstance(p, dict))


def test_hydrate_messages_drops_legacy_file_media_ref(tmp_path: Path):
    pdf = tmp_path / "spec.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")

    stored = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "[用户上传文件已保存到工作区] "
                    + str(pdf)
                    + "\nmime=application/pdf",
                },
                {
                    "type": "media_ref",
                    "media_type": "file",
                    "path": str(pdf),
                    "name": "spec.pdf",
                    "mime_type": "application/pdf",
                },
            ],
            "_turn_id": 1,
        },
    ]

    hydrated = hydrate_messages_for_api(
        stored,
        current_turn_id=1,
        vision_supported=True,
    )

    parts = hydrated[0]["content"]
    assert parts[0]["type"] == "text"
    assert all(p.get("type") != "media_ref" for p in parts if isinstance(p, dict))


def test_adapt_content_items_never_returns_inline_file_data():
    preface, adapted = adapt_content_items_for_provider(
        [
            {
                "type": "user_file",
                "name": "spec.pdf",
                "path": "/tmp/spec.pdf",
                "mime_type": "application/pdf",
                "file_data": "JVBERi0xLjc=",
            }
        ],
        supported_file_mime_types=["application/pdf"],
        enable_native_file_blocks=True,
    )
    assert "/tmp/spec.pdf" in preface
    assert adapted == []
