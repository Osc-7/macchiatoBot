"""飞书内容解析器测试。"""

from __future__ import annotations

import json

from frontend.feishu.content_parser import (
    feishu_message_should_queue_attachments,
    parse_feishu_message,
)


def test_feishu_message_should_queue_image_and_file() -> None:
    img_refs, _ = parse_feishu_message(
        "m1", "image", '{"image_key":"ik"}'
    )
    assert feishu_message_should_queue_attachments("image", img_refs)
    doc_refs, _ = parse_feishu_message(
        "m2", "file", '{"file_key":"fk","file_name":"a.pdf"}'
    )
    assert feishu_message_should_queue_attachments("file", doc_refs)


def test_feishu_message_should_not_queue_media() -> None:
    refs, text = parse_feishu_message(
        "m3",
        "media",
        '{"file_key":"fv","image_key":"ic","file_name":"v.mp4"}',
    )
    assert refs
    assert not feishu_message_should_queue_attachments("media", refs)


def test_feishu_message_post_never_queued() -> None:
    content_img_only = (
        '{"zh_cn":{"title":"","content":[[{"tag":"img","image_key":"img_only"}]]}}'
    )
    refs, _ = parse_feishu_message("m4", "post", content_img_only)
    assert refs
    assert not feishu_message_should_queue_attachments("post", refs)

    content = '{"zh_cn":{"title":"t","content":[[{"tag":"text","text":"说明"},{"tag":"img","image_key":"img_x"}]]}}'
    refs2, _ = parse_feishu_message("m5", "post", content)
    assert refs2
    assert not feishu_message_should_queue_attachments("post", refs2)


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


def test_parse_post_message_inline_link_and_at():
    """同段内 text + 超链接 + @（飞书文档示例结构）"""
    content = (
        '{"zh_cn":{"title":"","content":['
        '[{"tag":"text","text":"第一行:"},'
        '{"tag":"a","href":"http://www.feishu.cn","text":"超链接"},'
        '{"tag":"at","user_id":"ou_123","user_name":"Tom"}]'
        "]}}"
    )
    refs, text = parse_feishu_message(
        message_id="om_6",
        message_type="post",
        content=content,
    )
    assert refs == []
    assert "第一行:" in text
    assert "[超链接](http://www.feishu.cn)" in text
    assert "@Tom" in text


def test_parse_post_message_at_everyone():
    refs, text = parse_feishu_message(
        message_id="om_7",
        message_type="post",
        content='{"zh_cn":{"title":"","content":[[{"tag":"at","user_id":"all"}]]}}',
    )
    assert refs == []
    assert "@所有人" in text


def test_parse_post_message_ja_jp_locale():
    """仅非 zh_cn/en_us 语言键时仍能解析"""
    content = (
        '{"ja_jp":{"title":"日","content":[[{"tag":"text","text":"hello"}]]}}'
    )
    refs, text = parse_feishu_message(
        message_id="om_8",
        message_type="post",
        content=content,
    )
    assert refs == []
    assert "日" in text
    assert "hello" in text


def test_parse_post_message_video_media():
    content = (
        '{"zh_cn":{"title":"","content":['
        '[{"tag":"media","file_key":"file_v2_abc","image_key":"img_cover"}]'
        "]}}"
    )
    refs, text = parse_feishu_message(
        message_id="om_9",
        message_type="post",
        content=content,
    )
    assert len(refs) == 1
    assert refs[0].ref_type == "video"
    assert refs[0].key == "file_v2_abc"
    assert refs[0].extra.get("cover_image_key") == "img_cover"
    assert "[用户发送了一段视频]" in text


def test_parse_post_message_content_json_string():
    """content 偶发为 JSON 字符串时仍能解析"""
    inner = (
        '[[{"tag":"text","text":"line"}],[{"tag":"img","image_key":"img_str"}]]'
    )
    content = json.dumps(
        {"zh_cn": {"title": "", "content": inner}}
    )
    refs, text = parse_feishu_message(
        message_id="om_s",
        message_type="post",
        content=content,
    )
    assert len(refs) == 1
    assert refs[0].key == "img_str"
    assert "line" in text


def test_parse_post_message_receive_v1_flat_shape():
    """接收消息 API：顶层 title + content，无 zh_cn 包裹（与发送 JSON 不同）。"""
    content = json.dumps(
        {
            "title": "诶，原来可以这样",
            "content": [
                [{"tag": "img", "image_key": "img_recv_flat"}],
                [{"tag": "text", "text": "配图说明"}],
            ],
        },
        ensure_ascii=False,
    )
    refs, text = parse_feishu_message(
        message_id="om_recv_flat",
        message_type="post",
        content=content,
    )
    assert len(refs) == 1
    assert refs[0].key == "img_recv_flat"
    assert "诶，原来可以这样" in text
    assert "配图说明" in text


def test_parse_post_message_wrapped_under_post_key():
    """少数负载在 post 键下再嵌套 zh_cn 或扁平块。"""
    flat_inner = {
        "title": "",
        "content": [[{"tag": "text", "text": "nested"}]],
    }
    refs, text = parse_feishu_message(
        message_id="om_wrap",
        message_type="post",
        content=json.dumps({"post": flat_inner}, ensure_ascii=False),
    )
    assert refs == []
    assert "nested" in text
