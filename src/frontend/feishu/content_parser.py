"""飞书消息内容解析：将 image/file/media/audio 转为 ContentReference。"""

from __future__ import annotations

import json
import logging
from typing import List, Tuple

from agent_core.content import ContentReference

logger = logging.getLogger(__name__)

# message_type -> (ref_type, key_field, resource_type for API)
_FEISHU_CONTENT_MAP = {
    "image": ("image", "image_key", "image"),
    "file": ("document", "file_key", "file"),
    "media": ("video", "file_key", "file"),
    "audio": ("audio", "file_key", "file"),
}


def parse_feishu_content_to_refs(
    message_id: str,
    message_type: str,
    content: str,
) -> Tuple[List[ContentReference], str]:
    """
    解析飞书消息 content 为 ContentReference 列表及可选用户输入文本。

    Args:
        message_id: 消息 ID
        message_type: 飞书 message_type (text/image/file/media/audio/post/...)
        content: 消息 content JSON 字符串

    Returns:
        (content_refs, user_text)
        - content_refs: 解析出的 ContentReference 列表
        - user_text: 纯文本消息；若为纯媒体消息则返回占位描述
    """
    content_refs: List[ContentReference] = []
    user_text = ""

    mapping = _FEISHU_CONTENT_MAP.get(message_type)
    if not mapping:
        return [], ""

    ref_type, key_field, _resource_type = mapping

    try:
        data = json.loads(content) if isinstance(content, str) else content
    except Exception:
        data = {}

    key_val = (data.get(key_field) or "").strip()
    if not key_val:
        return [], ""

    extra = {"message_id": message_id}
    file_name = str(data.get("file_name") or "").strip()
    if file_name:
        extra["file_name"] = file_name

    ref = ContentReference(
        source="feishu",
        ref_type=ref_type,
        key=key_val,
        extra=extra,
    )
    content_refs.append(ref)

    # 纯媒体消息时，给 Agent 一个可理解的占位文本
    placeholders = {
        "image": "[用户发送了一张图片]",
        "document": "[用户发送了一个文件]",
        "video": "[用户发送了一段视频]",
        "audio": "[用户发送了一段音频]",
    }
    user_text = placeholders.get(ref_type, "[用户发送了媒体内容]")

    return content_refs, user_text


def parse_feishu_post_to_refs(
    message_id: str,
    content: str,
) -> Tuple[List[ContentReference], str]:
    """
    解析飞书 post 富文本消息中的内嵌图片与文本。

    飞书 post 结构: {"zh_cn": {"title": "...", "content": [[{"tag":"text","text":"..."}], [{"tag":"img","image_key":"img_xxx"}]]}}
    每段 content 是段落数组，段落内是标签数组；img 标签独占一段。
    """
    content_refs: List[ContentReference] = []
    text_parts: List[str] = []

    try:
        data = json.loads(content) if isinstance(content, str) else content
        if not isinstance(data, dict):
            return [], ""

        # 优先 zh_cn，fallback en_us
        lang = data.get("zh_cn") or data.get("en_us") or {}
        if not isinstance(lang, dict):
            return [], ""
        title = (lang.get("title") or "").strip()
        if title:
            text_parts.append(title)
        raw_content = lang.get("content")
        if not isinstance(raw_content, list):
            return content_refs, title or "[用户发送了富文本]"

        for para in raw_content:
            if not isinstance(para, list):
                continue
            for item in para:
                if not isinstance(item, dict):
                    continue
                tag = (item.get("tag") or "").strip()
                if tag == "img":
                    key = (item.get("image_key") or "").strip()
                    if key:
                        content_refs.append(
                            ContentReference(
                                source="feishu",
                                ref_type="image",
                                key=key,
                                extra={"message_id": message_id},
                            )
                        )
                elif tag == "text":
                    t = (item.get("text") or "").strip()
                    if t:
                        text_parts.append(t)

        user_text = "\n".join(text_parts).strip()
        if not user_text and content_refs:
            user_text = "[用户发送了一张图片]"
        if not user_text:
            user_text = "[用户发送了富文本]"
        return content_refs, user_text
    except Exception:
        return [], ""


def parse_feishu_message(
    message_id: str,
    message_type: str,
    content: str,
) -> Tuple[List[ContentReference], str]:
    """
    根据飞书消息类型解析出 content_refs 和 user_text。

    - text: 只返回 text 部分，无 content_refs
    - image/file/media/audio: 返回 content_refs + 占位 user_text
    - post: 解析富文本内嵌图片（img 标签）及文本
    - interactive 等: 目前不解析，仅返回空
    """
    if message_type == "text":
        try:
            data = json.loads(content) if isinstance(content, str) else content
            user_text = str(data.get("text", "") or "").strip()
        except Exception:
            user_text = str(content).strip()
        return [], user_text

    if message_type == "post":
        return parse_feishu_post_to_refs(message_id, content)

    return parse_feishu_content_to_refs(message_id, message_type, content)
