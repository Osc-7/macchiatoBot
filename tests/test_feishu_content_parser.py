"""飞书内容解析器测试。"""

from __future__ import annotations

from frontend.feishu.content_parser import parse_feishu_message


def test_parse_text_message():
    refs, text = parse_feishu_message(
        message_id="om_1",
        message_type="text",
        content='{"text":"明天早上8点开会"}',
    )
    assert refs == []
    assert text == "明天早上8点开会"


def test_parse_image_message():
    refs, text = parse_feishu_message(
        message_id="om_2",
        message_type="image",
        content='{"image_key":"img_xxx"}',
    )
    assert len(refs) == 1
    assert refs[0].source == "feishu"
    assert refs[0].ref_type == "image"
    assert refs[0].key == "img_xxx"
    assert refs[0].extra == {"message_id": "om_2"}
    assert text == "[用户发送了一张图片]"


def test_parse_media_message():
    refs, text = parse_feishu_message(
        message_id="om_3",
        message_type="media",
        content='{"file_key":"file_abc","image_key":"img_xyz","file_name":"vid.mp4","duration":2000}',
    )
    assert len(refs) == 1
    assert refs[0].source == "feishu"
    assert refs[0].ref_type == "video"
    assert refs[0].key == "file_abc"
    assert refs[0].extra == {"message_id": "om_3", "file_name": "vid.mp4"}
    assert text == "[用户发送了一段视频]"


def test_parse_file_message_keeps_original_filename():
    refs, text = parse_feishu_message(
        message_id="om_file",
        message_type="file",
        content='{"file_key":"file_pdf","file_name":"测试文档.pdf"}',
    )
    assert len(refs) == 1
    assert refs[0].ref_type == "document"
    assert refs[0].extra == {"message_id": "om_file", "file_name": "测试文档.pdf"}
    assert text == "[用户发送了一个文件]"


def test_parse_post_message_with_image():
    """富文本 post 消息内嵌图片解析"""
    content = '{"zh_cn":{"title":"架构图","content":[[{"tag":"text","text":"见下图"}],[{"tag":"img","image_key":"img_abc123"}]]}}'
    refs, text = parse_feishu_message(
        message_id="om_4",
        message_type="post",
        content=content,
    )
    assert len(refs) == 1
    assert refs[0].source == "feishu"
    assert refs[0].ref_type == "image"
    assert refs[0].key == "img_abc123"
    assert refs[0].extra == {"message_id": "om_4"}
    assert "架构图" in text
    assert "见下图" in text


def test_parse_post_message_image_only():
    """富文本 post 仅图片无文字"""
    content = (
        '{"zh_cn":{"title":"","content":[[{"tag":"img","image_key":"img_only"}]]}}'
    )
    refs, text = parse_feishu_message(
        message_id="om_5",
        message_type="post",
        content=content,
    )
    assert len(refs) == 1
    assert refs[0].key == "img_only"
    assert text == "[用户发送了一张图片]"
