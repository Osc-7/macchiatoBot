"""Tests for remote attachment inbox sync (file_blob_write + gateway hook helpers)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.agent.media_helpers import adapt_content_items_for_provider
from agent_core.remote.attachment_sync import (
    format_attachment_sync_notices,
    sync_content_items_to_remote_inbox,
    worker_supports_file_blob_write,
)
from agent_core.remote.workspace_state import (
    activate_remote_workspace,
    clear_remote_workspace_state,
)
from macchiato_remote.protocol import (
    REMOTE_BLOB_MAX_BYTES,
    REMOTE_PROTOCOL_VERSION,
    RemoteFileBlobWriteResult,
)
from macchiato_remote.runtime.files import write_workspace_blob
from macchiato_remote.runtime.macchiato_dir import INBOX_REL, ensure_macchiato_layout


def test_protocol_v4_declares_file_blob_write():
    from macchiato_remote.protocol import REMOTE_WORKER_CAPABILITIES

    assert REMOTE_PROTOCOL_VERSION == 4
    assert "file_blob_write" in REMOTE_WORKER_CAPABILITIES


def test_write_workspace_blob_roundtrip(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = b"\x00\x01\xffbinary"
    written, err = write_workspace_blob(
        workspace,
        f"{INBOX_REL}/sample.bin",
        base64.b64encode(payload).decode("ascii"),
    )
    assert err is None
    assert written == len(payload)
    assert (workspace / ".macchiato" / "inbox" / "sample.bin").read_bytes() == payload


def test_write_workspace_blob_rejects_oversize(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = b"x" * 64
    written, err = write_workspace_blob(
        workspace,
        f"{INBOX_REL}/big.bin",
        base64.b64encode(payload).decode("ascii"),
        max_bytes=16,
    )
    assert written == 0
    assert err is not None
    assert "BLOB_TOO_LARGE" in err


def test_ensure_macchiato_layout_creates_inbox(tmp_path: Path):
    root = tmp_path / "ws"
    paths = ensure_macchiato_layout(root, device_label="lab")
    assert (root / ".macchiato" / "inbox").is_dir()
    assert paths["inbox_dir"] == str(root.resolve() / ".macchiato" / "inbox")


def test_worker_supports_file_blob_write_cap():
    assert worker_supports_file_blob_write(
        protocol_version=4, capabilities=["file_blob_write"]
    )
    assert not worker_supports_file_blob_write(
        protocol_version=3, capabilities=["file_blob_read"]
    )
    # Capability present wins even on older protocol_version field.
    assert worker_supports_file_blob_write(
        protocol_version=3, capabilities=["file_blob_write"]
    )


def test_adapt_prefers_remote_path_in_preface():
    preface, adapted = adapt_content_items_for_provider(
        [
            {
                "type": "user_file",
                "path": "/daemon/uploads/feishu/doc.pdf",
                "remote_path": ".macchiato/inbox/doc.pdf",
                "mime_type": "application/pdf",
                "name": "doc.pdf",
            },
            {
                "type": "media_ref",
                "media_type": "image",
                "path": "/daemon/uploads/feishu/pic.png",
                "remote_path": ".macchiato/inbox/pic.png",
                "name": "pic.png",
            },
        ]
    )
    assert ".macchiato/inbox/doc.pdf" in preface
    assert "/daemon/uploads/feishu/doc.pdf" not in preface
    assert ".macchiato/inbox/pic.png" in preface
    assert adapted[0]["path"] == "/daemon/uploads/feishu/pic.png"
    assert adapted[0]["remote_path"] == ".macchiato/inbox/pic.png"


@pytest.mark.asyncio
async def test_sync_mirrors_to_inbox_and_rewrites_text(tmp_path: Path):
    clear_remote_workspace_state()
    sid = "sess-sync-ok"
    activate_remote_workspace(
        session_id=sid,
        login="personal",
        requested_path=str(tmp_path / "remote"),
    )
    local = tmp_path / "uploads" / "note.pdf"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"%PDF-1.4 mock")

    written: Dict[str, Any] = {}

    async def _blob_write(**kwargs):
        written.update(kwargs)
        return RemoteFileBlobWriteResult(
            request_id="r1",
            path=kwargs["path"],
            bytes_written=len(base64.b64decode(kwargs["content_base64"])),
        )

    mock_conn = MagicMock()
    mock_conn.hello_meta = {
        "protocol_version": 4,
        "capabilities": ["file_blob_write"],
    }
    mock_registry = MagicMock()
    mock_registry.get = AsyncMock(return_value=mock_conn)
    mock_registry.file_blob_write = AsyncMock(side_effect=_blob_write)

    items = [
        {
            "type": "user_file",
            "path": str(local),
            "name": "note.pdf",
            "mime_type": "application/pdf",
        },
        {
            "type": "text",
            "text": f"[用户上传文件已保存到工作区] {local}",
        },
    ]
    with patch(
        "agent_core.remote.worker_registry.get_remote_worker_registry",
        return_value=mock_registry,
    ):
        out_items, out_text, notices = await sync_content_items_to_remote_inbox(
            session_id=sid,
            content_items=items,
            user_text=f"请看 {local}",
        )

    assert written["path"] == f"{INBOX_REL}/note.pdf"
    assert out_items[0]["path"] == str(local)
    assert out_items[0]["remote_path"] == f"{INBOX_REL}/note.pdf"
    assert f"{INBOX_REL}/note.pdf" in out_items[1]["text"]
    assert str(local) not in out_items[1]["text"]
    assert f"{INBOX_REL}/note.pdf" in out_text
    assert any("已同步" in n for n in notices)
    clear_remote_workspace_state()


@pytest.mark.asyncio
async def test_sync_reopens_worker_session_on_not_open(tmp_path: Path):
    clear_remote_workspace_state()
    sid = "sess-sync-reopen"
    activate_remote_workspace(
        session_id=sid,
        login="personal",
        requested_path=str(tmp_path / "remote"),
    )
    local = tmp_path / "uploads" / "clip.png"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"\x89PNG mock")

    calls = {"blob": 0, "open": 0}

    async def _blob_write(**kwargs):
        calls["blob"] += 1
        if calls["blob"] == 1:
            return RemoteFileBlobWriteResult(
                request_id="r1",
                path=kwargs["path"],
                error="SESSION_NOT_OPEN",
            )
        return RemoteFileBlobWriteResult(
            request_id="r2",
            path=kwargs["path"],
            bytes_written=len(base64.b64decode(kwargs["content_base64"])),
        )

    async def _open_workspace(**kwargs):
        calls["open"] += 1
        from macchiato_remote.protocol import RemoteWorkspaceOpenResult

        return RemoteWorkspaceOpenResult(
            request_id="open1",
            session_id=kwargs["session_id"],
            success=True,
            resolved_path=str(tmp_path / "remote"),
        )

    mock_conn = MagicMock()
    mock_conn.hello_meta = {
        "protocol_version": 4,
        "capabilities": ["file_blob_write"],
    }
    mock_registry = MagicMock()
    mock_registry.get = AsyncMock(return_value=mock_conn)
    mock_registry.file_blob_write = AsyncMock(side_effect=_blob_write)
    mock_registry.open_workspace = AsyncMock(side_effect=_open_workspace)

    items = [
        {
            "type": "media_ref",
            "media_type": "image",
            "path": str(local),
            "name": "clip.png",
        }
    ]
    with patch(
        "agent_core.remote.worker_registry.get_remote_worker_registry",
        return_value=mock_registry,
    ):
        out_items, _out_text, notices = await sync_content_items_to_remote_inbox(
            session_id=sid,
            content_items=items,
        )

    assert calls["open"] == 1
    assert calls["blob"] == 2
    assert out_items[0]["remote_path"] == f"{INBOX_REL}/clip.png"
    assert any("已同步" in n for n in notices)
    clear_remote_workspace_state()


@pytest.mark.asyncio
async def test_sync_skips_when_worker_missing_cap(tmp_path: Path):
    clear_remote_workspace_state()
    sid = "sess-sync-old"
    activate_remote_workspace(
        session_id=sid,
        login="personal",
        requested_path=str(tmp_path / "remote"),
    )
    local = tmp_path / "a.bin"
    local.write_bytes(b"abc")

    mock_conn = MagicMock()
    mock_conn.hello_meta = {
        "protocol_version": 3,
        "capabilities": ["file_blob_read"],
    }
    mock_registry = MagicMock()
    mock_registry.get = AsyncMock(return_value=mock_conn)
    mock_registry.file_blob_write = AsyncMock()

    with patch(
        "agent_core.remote.worker_registry.get_remote_worker_registry",
        return_value=mock_registry,
    ):
        out_items, _text, notices = await sync_content_items_to_remote_inbox(
            session_id=sid,
            content_items=[{"type": "media_ref", "path": str(local), "name": "a.bin"}],
        )

    mock_registry.file_blob_write.assert_not_called()
    assert "remote_path" not in out_items[0]
    assert any("0.2.10" in n for n in notices)
    assert "升级" in format_attachment_sync_notices(notices)
    clear_remote_workspace_state()


@pytest.mark.asyncio
async def test_sync_skips_oversize_file(tmp_path: Path):
    clear_remote_workspace_state()
    sid = "sess-sync-big"
    activate_remote_workspace(
        session_id=sid,
        login="personal",
        requested_path=str(tmp_path / "remote"),
    )
    local = tmp_path / "huge.bin"
    local.write_bytes(b"x" * 100)

    mock_conn = MagicMock()
    mock_conn.hello_meta = {
        "protocol_version": 4,
        "capabilities": ["file_blob_write"],
    }
    mock_registry = MagicMock()
    mock_registry.get = AsyncMock(return_value=mock_conn)
    mock_registry.file_blob_write = AsyncMock()

    with patch(
        "agent_core.remote.worker_registry.get_remote_worker_registry",
        return_value=mock_registry,
    ):
        out_items, _text, notices = await sync_content_items_to_remote_inbox(
            session_id=sid,
            content_items=[{"type": "user_file", "path": str(local), "name": "huge.bin"}],
            max_bytes=16,
        )

    mock_registry.file_blob_write.assert_not_called()
    assert "remote_path" not in out_items[0]
    assert any("过大" in n for n in notices)
    # sanity: default cap still large
    assert REMOTE_BLOB_MAX_BYTES >= 20 * 1024 * 1024
    from macchiato_remote.protocol import REMOTE_WS_MAX_SIZE

    # websockets 默认 1MiB；WS 上限需覆盖 base64(~4/3) + JSON 开销。
    assert REMOTE_WS_MAX_SIZE > REMOTE_BLOB_MAX_BYTES * 4 // 3
    assert REMOTE_WS_MAX_SIZE > 2**20
    clear_remote_workspace_state()
