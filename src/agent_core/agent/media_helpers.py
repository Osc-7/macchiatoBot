"""Helpers for multimodal carry-over and file/media input adaptation."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from agent_core.tools import ToolResult
from agent_core.utils.media import resolve_media_to_content_item


def queue_media_for_next_call(
    result: ToolResult,
    pending_multimodal_items: List[Dict[str, Any]],
    media_resolver: Callable[
        [str], Tuple[Dict[str, Any] | None, str | None]
    ] = resolve_media_to_content_item,
) -> None:
    """Queue media declared by a tool result into the next LLM call payload."""
    if not result.success:
        return
    if not isinstance(result.metadata, dict):
        return
    if not result.metadata.get("embed_in_next_call"):
        return

    candidate_paths: List[str] = []
    data = result.data
    if isinstance(data, dict):
        path = data.get("path")
        if isinstance(path, str) and path.strip():
            candidate_paths.append(path.strip())
        paths = data.get("paths")
        if isinstance(paths, list):
            for item in paths:
                if isinstance(item, str) and item.strip():
                    candidate_paths.append(item.strip())

    meta_path = result.metadata.get("path")
    if isinstance(meta_path, str) and meta_path.strip():
        candidate_paths.append(meta_path.strip())
    meta_paths = result.metadata.get("paths")
    if isinstance(meta_paths, list):
        for item in meta_paths:
            if isinstance(item, str) and item.strip():
                candidate_paths.append(item.strip())

    for media_path in candidate_paths:
        content_item, _err = media_resolver(media_path)
        if content_item:
            pending_multimodal_items.append(content_item)


def collect_outgoing_attachment(
    result: ToolResult,
    outgoing_attachments: List[Dict[str, Any]],
) -> None:
    """Collect user-facing attachment metadata from tool results."""
    if not result.success or not isinstance(result.metadata, dict):
        return
    att = result.metadata.get("outgoing_attachment")
    if not att or not isinstance(att, dict):
        return
    if att.get("type") not in {"image", "file"}:
        return
    if "path" in att or "url" in att:
        outgoing_attachments.append(dict(att))


def append_pending_multimodal_messages(
    messages: List[Dict[str, Any]],
    pending_multimodal_items: List[Dict[str, Any]],
    *,
    vision_supported: bool = True,
    unseen_media: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Append pending media as one extra user multimodal message for this request only.

    当 ``vision_supported=False`` 时，不再挂载 image/video 数据，而是把它们折叠成
    纯文字占位提示，同时将原始条目登记到 ``unseen_media`` 里，供 ``recognize_image``
    工具按 name/url 回查。
    """
    if not pending_multimodal_items:
        return messages

    if vision_supported:
        content: List[Dict[str, Any]] = [
            {
                "type": "text",
                "text": "以下是你在上一轮工具调用中请求附加的媒体，请结合当前任务继续分析。",
            }
        ]
        content.extend(pending_multimodal_items)
        return [*messages, {"role": "user", "content": content}]

    placeholder_text, kept_items = _downgrade_media_items_to_text(
        pending_multimodal_items,
        unseen_media=unseen_media,
        name_prefix="pending_media",
    )
    header = (
        "上一轮工具调用请求附加了以下媒体，但当前主模型不具备视觉能力，"
        "所以只给你文字占位。如需了解内容，请调用 recognize_image 工具："
    )
    text = header + "\n" + placeholder_text if placeholder_text else header
    new_msg: Dict[str, Any]
    if kept_items:
        parts: List[Dict[str, Any]] = [{"type": "text", "text": text}]
        parts.extend(kept_items)
        new_msg = {"role": "user", "content": parts}
    else:
        new_msg = {"role": "user", "content": text}
    return [*messages, new_msg]


def adapt_content_items_for_provider(
    content_items: Optional[List[Dict[str, Any]]],
    *,
    supported_file_mime_types: Optional[List[str]] = None,
    enable_native_file_blocks: bool = False,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Adapt generic content items to the active provider's native input schema.

    Returns:
        (preface_text, adapted_items)

        ``preface_text`` 用于把「文件已保存到工作区」之类的提示拼进 user 文本；
        ``adapted_items`` 则为可直接注入到本轮 user message.content 的内容块。
    """
    if not content_items:
        return "", []

    supported = {
        str(m).strip().lower()
        for m in (supported_file_mime_types or [])
        if str(m).strip()
    }
    text_lines: List[str] = []
    adapted: List[Dict[str, Any]] = []

    for raw in content_items:
        if not isinstance(raw, dict):
            continue
        if raw.get("type") != "user_file":
            adapted.append(raw)
            continue

        mime = str(raw.get("mime_type") or "application/octet-stream").strip().lower()
        path = str(raw.get("path") or "").strip()
        name = str(raw.get("name") or "").strip() or "attachment.bin"
        file_data = str(raw.get("file_data") or "").strip()
        preview_text = str(raw.get("preview_text") or "").strip()

        if path:
            text_lines.append(f"[用户上传文件已保存到工作区] {path}")
        if mime:
            text_lines.append(f"mime={mime}")

        if enable_native_file_blocks and file_data and mime in supported:
            adapted.append(
                {
                    "type": "file",
                    "file": {
                        "filename": name,
                        "file_data": file_data,
                    },
                    "mime_type": mime,
                    "path": path,
                    "name": name,
                }
            )
            continue

        if preview_text:
            text_lines.append("以下是文件内容预览：")
            text_lines.append(preview_text)
        else:
            text_lines.append("该文件类型当前不会直接挂载给模型，请按路径读取后再处理。")

    return "\n".join(text_lines).strip(), adapted


def _downgrade_media_items_to_text(
    content_items: List[Dict[str, Any]],
    *,
    unseen_media: Optional[List[Dict[str, Any]]] = None,
    name_prefix: str = "image",
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    把 image_url/video_url content items 折叠为文字占位，并登记到 unseen_media。

    Returns:
        (placeholder_text, kept_items)

        - ``placeholder_text``：形如 ``[用户附上图片 name=image_1 ...]，如需理解调用
          recognize_image 工具`` 的多行文本，用来拼到 user 文本里。
        - ``kept_items``：保留的非 image/video items（一般为空）。
    """
    lines: List[str] = []
    kept: List[Dict[str, Any]] = []
    seq_image = 0
    seq_video = 0
    for raw in content_items or []:
        if not isinstance(raw, dict):
            continue
        t = raw.get("type")
        if t == "image_url":
            seq_image += 1
            url = ""
            inner = raw.get("image_url")
            if isinstance(inner, dict):
                url = str(inner.get("url") or "").strip()
            name = str(raw.get("name") or "").strip() or f"{name_prefix}_{seq_image}"
            path = str(raw.get("path") or "").strip()
            if unseen_media is not None:
                unseen_media.append(
                    {
                        "name": name,
                        "path": path,
                        "url": url,
                        "media_type": "image",
                    }
                )
            segs = [f"name={name}"]
            if path:
                segs.append(f"path={path}")
            lines.append(
                f"[用户附上图片 {' '.join(segs)}]，如需理解调用 recognize_image 工具"
            )
        elif t == "video_url":
            seq_video += 1
            url = ""
            inner = raw.get("video_url")
            if isinstance(inner, dict):
                url = str(inner.get("url") or "").strip()
            name = str(raw.get("name") or "").strip() or f"video_{seq_video}"
            path = str(raw.get("path") or "").strip()
            if unseen_media is not None:
                unseen_media.append(
                    {
                        "name": name,
                        "path": path,
                        "url": url,
                        "media_type": "video",
                    }
                )
            segs = [f"name={name}"]
            if path:
                segs.append(f"path={path}")
            lines.append(
                f"[用户附上视频 {' '.join(segs)}]，当前模型不具备视频能力，请据此文字提示进行说明"
            )
        else:
            kept.append(raw)
    return "\n".join(lines), kept


def downgrade_user_media_to_text(
    content_items: Optional[List[Dict[str, Any]]],
    *,
    unseen_media: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    对用户本轮输入的多模态 content_items 做视觉降级。

    当主模型不具备 vision 时由 ``AgentCore.prepare_turn`` 调用：
    图像/视频被转成文字占位拼到 user text 里，原始 data url/路径登记到
    ``unseen_media`` 供 ``recognize_image`` 工具按 name 回查。

    Returns:
        (placeholder_text, kept_items) — 占位文字 + 非图像/视频的剩余 items
    """
    if not content_items:
        return "", []
    return _downgrade_media_items_to_text(
        content_items, unseen_media=unseen_media, name_prefix="image"
    )
