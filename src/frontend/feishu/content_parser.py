"""飞书消息内容解析：将 image/file/media/audio 转为 ContentReference。"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from agent_core.content import ContentReference

logger = logging.getLogger(__name__)


def feishu_message_should_queue_attachments(
    message_type: str,
    content_refs: List[ContentReference],
) -> bool:
    """
    是否应暂缓 Agent，仅把附件排入「下一轮对话」。

    仅独立 ``image`` / ``file`` 消息入队；``post`` 等富文本仍立即解析并走对话。
    """
    if not content_refs:
        return False
    mt = (message_type or "").strip()
    return mt in ("image", "file")


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


def _normalize_post_content_list(raw: Any) -> Optional[list]:
    """飞书文档中 content 为段落数组；少数情况下可能是 JSON 字符串。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def _pick_post_locale_block(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    选取一种语言的富文本块。

    - **接收消息**（im.message.receive_v1 / 消息查询）：post 为顶层 ``title`` + ``content``，
      无 ``zh_cn`` 包裹，见
      https://open.feishu.cn/document/server-docs/im-v1/message-content-description/message_content
    - **发送接口**风格：``{zh_cn: {title, content}, ...}``
    """
    # 1) 扁平接收格式：顶层即段落数组
    if _normalize_post_content_list(data.get("content")) is not None:
        return data

    # 2) 少数负载把正文包在 post 键下
    post_wrap = data.get("post")
    if isinstance(post_wrap, dict):
        inner = _pick_post_locale_block(post_wrap)
        if inner is not None:
            return inner

    for key in ("zh_cn", "en_us"):
        block = data.get(key)
        if isinstance(block, dict) and _normalize_post_content_list(block.get("content")):
            return block
    for _k, block in data.items():
        if isinstance(block, dict) and _normalize_post_content_list(block.get("content")):
            return block
    for key in ("zh_cn", "en_us"):
        block = data.get(key)
        if isinstance(block, dict):
            return block
    for _k, block in data.items():
        if isinstance(block, dict):
            return block
    return None


def _post_item_to_para_chunks(
    message_id: str,
    item: Dict[str, Any],
    content_refs: List[ContentReference],
    para_chunks: List[str],
) -> None:
    """将单个 post 元素转为段内文本片段，并视情况写入 content_refs（图片/视频）。"""
    tag = (item.get("tag") or "").strip()
    if tag == "text":
        t = item.get("text")
        if t is not None and str(t) != "":
            # 保留飞书下发的空白，避免相邻 text 节点 intentional 空格丢失
            para_chunks.append(str(t))
        return
    if tag == "a":
        text = str(item.get("text") or "").strip()
        href = str(item.get("href") or "").strip()
        if text and href:
            para_chunks.append(f"[{text}]({href})")
        elif href:
            para_chunks.append(href)
        elif text:
            para_chunks.append(text)
        return
    if tag == "at":
        user_id = str(item.get("user_id") or "").strip()
        user_name = str(item.get("user_name") or "").strip()
        if user_id == "all":
            para_chunks.append("@所有人")
        elif user_name:
            para_chunks.append(f"@{user_name}")
        elif user_id:
            para_chunks.append(f"@{user_id}")
        return
    if tag == "img":
        key = str(item.get("image_key") or "").strip()
        if key:
            content_refs.append(
                ContentReference(
                    source="feishu",
                    ref_type="image",
                    key=key,
                    extra={"message_id": message_id},
                )
            )
        return
    if tag == "media":
        file_key = str(item.get("file_key") or "").strip()
        image_key = str(item.get("image_key") or "").strip()
        extra: Dict[str, str] = {"message_id": message_id}
        if image_key:
            extra["cover_image_key"] = image_key
        if file_key:
            content_refs.append(
                ContentReference(
                    source="feishu",
                    ref_type="video",
                    key=file_key,
                    extra=extra,
                )
            )
            para_chunks.append("[用户发送了一段视频]")
        return
    if tag == "emotion":
        emoji_type = str(item.get("emoji_type") or "").strip()
        if emoji_type:
            para_chunks.append(f"[表情:{emoji_type}]")
        return
    if tag == "hr":
        para_chunks.append("\n---\n")
        return
    if tag == "code_block":
        language = str(item.get("language") or "").strip()
        body = item.get("text")
        body_s = "" if body is None else str(body)
        fence_lang = language.lower() if language else ""
        para_chunks.append(f"\n```{fence_lang}\n{body_s}\n```\n")
        return
    if tag == "md":
        md_text = item.get("text")
        if md_text is not None and str(md_text) != "":
            para_chunks.append(str(md_text))
        return
    logger.debug("feishu post: unsupported tag=%s keys=%s", tag, list(item.keys()))


def parse_feishu_post_to_refs(
    message_id: str,
    content: str,
) -> Tuple[List[ContentReference], str]:
    """
    解析飞书 post 富文本消息中的内嵌图片/视频与可读文本。

    飞书结构见文档「富文本 post」：
    https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/im-v1/message/create_json

    支持 tag: text, a, at, img, media, emotion, hr, code_block, md
    （与同段多元素内联拼接；图片/视频按文档应独占段落）
    """
    content_refs: List[ContentReference] = []
    text_parts: List[str] = []

    try:
        data = json.loads(content) if isinstance(content, str) else content
        if not isinstance(data, dict):
            return [], ""

        lang = _pick_post_locale_block(data)
        if not lang:
            return [], ""

        title = str(lang.get("title") or "").strip()
        if title:
            text_parts.append(title)

        raw_content = _normalize_post_content_list(lang.get("content"))
        if raw_content is None:
            return content_refs, title or "[用户发送了富文本]"

        for para in raw_content:
            if not isinstance(para, list):
                continue
            para_chunks: List[str] = []
            for item in para:
                if not isinstance(item, dict):
                    continue
                _post_item_to_para_chunks(
                    message_id, item, content_refs, para_chunks
                )
            line = "".join(para_chunks).strip()
            if line:
                text_parts.append(line)

        user_text = "\n".join(text_parts).strip()
        if not user_text and content_refs:
            only_images = all(r.ref_type == "image" for r in content_refs)
            if only_images:
                user_text = "[用户发送了一张图片]"
            elif all(r.ref_type == "video" for r in content_refs):
                user_text = "[用户发送了一段视频]"
            elif any(r.ref_type == "video" for r in content_refs) and any(
                r.ref_type == "image" for r in content_refs
            ):
                user_text = "[用户发送了图片与视频]"
            else:
                user_text = "[用户发送了富媒体]"
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
    - post: 解析富文本（text / a / at / img / media / emotion / hr / code_block / md）
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
