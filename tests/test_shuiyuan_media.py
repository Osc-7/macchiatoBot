"""
水源社区媒体功能测试：
- content_parser：解析 upload:// 图片引用
- session._upload_and_embed_attachments：附件上传与 Markdown 拼接
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from frontend.shuiyuan_integration.content_parser import parse_shuiyuan_raw_images


# ─── content_parser ──────────────────────────────────────────────────────────


def test_parse_single_image():
    raw = "看看这张图 ![image|690x188](upload://abc123.png) 怎么样"
    refs, cleaned = parse_shuiyuan_raw_images(raw)

    assert len(refs) == 1
    assert refs[0].source == "shuiyuan"
    assert refs[0].ref_type == "image"
    assert refs[0].key == "https://shuiyuan.sjtu.edu.cn/uploads/short-url/abc123.png"
    assert "[图片]" in cleaned
    assert "upload://" not in cleaned


def test_parse_multiple_images():
    raw = (
        "图1：![a](upload://img1.jpg)\n"
        "图2：![b|400x300](upload://img2.gif)\n"
        "普通文字"
    )
    refs, cleaned = parse_shuiyuan_raw_images(raw)

    assert len(refs) == 2
    assert refs[0].key.endswith("img1.jpg")
    assert refs[1].key.endswith("img2.gif")
    assert cleaned.count("[图片]") == 2
    assert "普通文字" in cleaned


def test_parse_no_images():
    raw = "没有图片的普通帖子 @macchiato 【玛奇朵】来聊聊天"
    refs, cleaned = parse_shuiyuan_raw_images(raw)

    assert refs == []
    assert cleaned == raw


def test_parse_custom_site_url():
    raw = "![x](upload://test.png)"
    refs, _ = parse_shuiyuan_raw_images(raw, site_url="https://example.com")

    assert refs[0].key == "https://example.com/uploads/short-url/test.png"


def test_parse_lightbox_download_href_and_secure_upload():
    raw = (
        '<div class="lightbox-wrapper">'
        '<a class="lightbox" href="https://shuiyuan.sjtu.edu.cn/secure-uploads/original/4X/4/5/a/img.png" '
        'data-download-href="/uploads/short-url/ABC123.png?dl=1" title="image">'
        '<img src="https://shuiyuan.s3.jcloud.sjtu.edu.cn/optimized/4X/4/5/a/img_2_690x448.jpeg" alt="image">'
        "</a></div>"
    )
    refs, cleaned = parse_shuiyuan_raw_images(raw)

    # 至少应解析出一个 uploads/short-url 引用（用于稳定访问图片）
    keys = {r.key for r in refs}
    assert any("/uploads/short-url/ABC123.png" in k for k in keys)
    # 文本中不再出现原始 URL 片段
    assert "uploads/short-url/ABC123" not in cleaned


def test_parse_empty_raw():
    refs, cleaned = parse_shuiyuan_raw_images("")
    assert refs == []
    assert cleaned == ""


# ─── _upload_and_embed_attachments ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_image_path_success(tmp_path):
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n")

    mock_client = MagicMock()
    mock_client.upload_file.return_value = {
        "short_url": "upload://abcdef.png",
        "width": 100,
        "height": 80,
    }

    from frontend.shuiyuan_integration.session import _upload_and_embed_attachments

    result = await _upload_and_embed_attachments(
        [{"type": "image", "path": str(img)}],
        client=mock_client,
    )

    assert "upload://abcdef.png" in result
    assert "![image|100x80]" in result
    mock_client.upload_file.assert_called_once()


@pytest.mark.asyncio
async def test_upload_missing_file_skips(tmp_path):
    mock_client = MagicMock()

    from frontend.shuiyuan_integration.session import _upload_and_embed_attachments

    result = await _upload_and_embed_attachments(
        [{"type": "image", "path": str(tmp_path / "nonexistent.png")}],
        client=mock_client,
    )

    assert result == ""
    mock_client.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_upload_returns_empty_on_no_attachments():
    mock_client = MagicMock()

    from frontend.shuiyuan_integration.session import _upload_and_embed_attachments

    result = await _upload_and_embed_attachments([], client=mock_client)

    assert result == ""


@pytest.mark.asyncio
async def test_upload_non_image_attachment_skipped(tmp_path):
    mock_client = MagicMock()

    from frontend.shuiyuan_integration.session import _upload_and_embed_attachments

    result = await _upload_and_embed_attachments(
        [{"type": "video", "path": "/some/video.mp4"}],
        client=mock_client,
    )

    assert result == ""
    mock_client.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_upload_failure_returns_empty(tmp_path):
    img = tmp_path / "fail.png"
    img.write_bytes(b"\x89PNG\r\n")

    mock_client = MagicMock()
    mock_client.upload_file.return_value = None  # 上传失败

    from frontend.shuiyuan_integration.session import _upload_and_embed_attachments

    result = await _upload_and_embed_attachments(
        [{"type": "image", "path": str(img)}],
        client=mock_client,
    )

    assert result == ""
