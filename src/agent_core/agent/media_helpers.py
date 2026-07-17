"""Helpers for multimodal carry-over and file/media input adaptation."""

from __future__ import annotations

import base64
import copy
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from agent_core.tools import ToolResult
from agent_core.utils.media import resolve_media_to_content_item

if TYPE_CHECKING:
    from agent_core.llm.vendor_files.kimi import KimiVendorFilesClient

_DATA_URL_PREFIX = "data:"


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
    media_items = result.metadata.get("media_items")
    if isinstance(media_items, list) and media_items:
        for raw in media_items:
            if isinstance(raw, dict):
                pending_multimodal_items.append(dict(raw))
        return

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
    if "path" in att or "url" in att or "content_base64" in att:
        outgoing_attachments.append(dict(att))


def append_pending_multimodal_messages(
    messages: List[Dict[str, Any]],
    pending_multimodal_items: List[Dict[str, Any]],
    *,
    vision_supported: bool = True,
    unseen_media: Optional[List[Dict[str, Any]]] = None,
    turn_id: Optional[int] = None,
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
        content.extend(
            normalize_media_items_for_context(pending_multimodal_items)
        )
        new_msg: Dict[str, Any] = {"role": "user", "content": content}
        if turn_id is not None:
            new_msg["_turn_id"] = turn_id
        return [*messages, new_msg]

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
        parts.extend(normalize_media_items_for_context(kept_items))
        new_msg = {"role": "user", "content": parts}
    else:
        new_msg = {"role": "user", "content": text}
    if turn_id is not None:
        new_msg["_turn_id"] = turn_id
    return [*messages, new_msg]


def adapt_content_items_for_provider(
    content_items: Optional[List[Dict[str, Any]]],
    *,
    supported_file_mime_types: Optional[List[str]] = None,
    enable_native_file_blocks: bool = False,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Adapt generic content items for user message preface + lightweight refs.

    Returns:
        (preface_text, adapted_items)

        ``preface_text`` 用于把「文件已保存到工作区」之类的提示拼进 user 文本；
        ``adapted_items`` 为不含 base64 的 ``media_ref`` 条目，写入对话上下文；
        实际二进制仅在 ``hydrate_messages_for_api`` 中按当前 turn 临时注入 API。
    """
    _ = enable_native_file_blocks, supported_file_mime_types
    if not content_items:
        return "", []

    text_lines: List[str] = []
    adapted: List[Dict[str, Any]] = []

    for raw in content_items:
        if not isinstance(raw, dict):
            continue
        if raw.get("type") != "user_file":
            remote_path = str(raw.get("remote_path") or "").strip()
            if remote_path and raw.get("type") in {"media_ref", "image_url", "video_url"}:
                text_lines.append(f"[用户附件已同步到远程工作区] {remote_path}")
            adapted.append(raw)
            continue

        mime = str(raw.get("mime_type") or "application/octet-stream").strip().lower()
        path = str(raw.get("path") or "").strip()
        remote_path = str(raw.get("remote_path") or "").strip()
        display_path = remote_path or path
        name = str(raw.get("name") or "").strip() or "attachment.bin"
        preview_text = str(raw.get("preview_text") or "").strip()

        if display_path:
            text_lines.append(f"[用户上传文件已保存到工作区] {display_path}")
            if remote_path:
                text_lines.append("（已同步到远程工作区）")
        if mime:
            text_lines.append(f"mime={mime}")

        if preview_text:
            text_lines.append("以下是文件内容预览：")
            text_lines.append(preview_text)
        elif display_path:
            text_lines.append("该文件已保存到工作区，请按路径读取或使用支持的文件接口处理。")

        # PDF/文档不在 messages 里挂载；路径提示已写入 preface 文本。

    return "\n".join(text_lines).strip(), adapted


def _is_data_url(value: str) -> bool:
    return isinstance(value, str) and value.strip().startswith(_DATA_URL_PREFIX)


def _guess_mime(path: str, explicit: str = "") -> str:
    mime = str(explicit or "").strip().lower()
    if mime and mime != "application/octet-stream":
        return mime
    guessed, _ = mimetypes.guess_type(path)
    return (guessed or mime or "application/octet-stream").lower()


def normalize_media_items_for_context(
    content_items: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    将多模态 content items 规范为仅含 path/url 的 ``media_ref``，不含 base64。
    """
    out: List[Dict[str, Any]] = []
    for raw in content_items or []:
        if not isinstance(raw, dict):
            continue
        t = raw.get("type")
        if t == "media_ref":
            out.append(
                {
                    "type": "media_ref",
                    "media_type": str(raw.get("media_type") or "file"),
                    "path": str(raw.get("path") or "").strip(),
                    "remote_path": str(raw.get("remote_path") or "").strip(),
                    "url": str(raw.get("url") or "").strip(),
                    "name": str(raw.get("name") or "").strip(),
                    "mime_type": str(raw.get("mime_type") or "").strip(),
                }
            )
            continue
        if t == "user_file":
            # 路径信息由 adapt_content_items_for_provider 写入文本 preface，不存 media_ref。
            continue
        if t == "image_url":
            path = str(raw.get("path") or "").strip()
            remote_path = str(raw.get("remote_path") or "").strip()
            url = ""
            inner = raw.get("image_url")
            if isinstance(inner, dict):
                url = str(inner.get("url") or "").strip()
            if path or (url and not _is_data_url(url)):
                out.append(
                    {
                        "type": "media_ref",
                        "media_type": "image",
                        "path": path,
                        "remote_path": remote_path,
                        "url": url if not _is_data_url(url) else "",
                        "name": str(raw.get("name") or "").strip(),
                        "mime_type": _guess_mime(path, "image/png"),
                    }
                )
            continue
        if t == "video_url":
            path = str(raw.get("path") or "").strip()
            remote_path = str(raw.get("remote_path") or "").strip()
            url = ""
            inner = raw.get("video_url")
            if isinstance(inner, dict):
                url = str(inner.get("url") or "").strip()
            if path or (url and not _is_data_url(url)):
                out.append(
                    {
                        "type": "media_ref",
                        "media_type": "video",
                        "path": path,
                        "remote_path": remote_path,
                        "url": url if not _is_data_url(url) else "",
                        "name": str(raw.get("name") or "").strip(),
                        "mime_type": _guess_mime(path, "video/mp4"),
                    }
                )
            continue
        if t == "file":
            path = str(raw.get("path") or "").strip()
            remote_path = str(raw.get("remote_path") or "").strip()
            name = str(raw.get("name") or "").strip()
            file_obj = raw.get("file") if isinstance(raw.get("file"), dict) else {}
            if not name and isinstance(file_obj, dict):
                name = str(file_obj.get("filename") or "").strip()
            mime = _guess_mime(path, str(raw.get("mime_type") or ""))
            if path:
                out.append(
                    {
                        "type": "media_ref",
                        "media_type": "file",
                        "path": path,
                        "remote_path": remote_path,
                        "name": name,
                        "mime_type": mime,
                    }
                )
            continue
        if t == "text":
            out.append(raw)
        else:
            out.append(raw)
    return out


def _read_path_as_base64(path: str) -> Optional[str]:
    p = Path(path)
    if not p.is_file():
        return None
    return base64.b64encode(p.read_bytes()).decode("ascii")


def _hydrate_media_ref_for_api(
    item: Dict[str, Any],
    *,
    vision_supported: bool,
    enable_native_file_blocks: bool,
    supported_file_mime_types: set[str],
    kimi_files: Optional["KimiVendorFilesClient"] = None,
) -> Optional[Dict[str, Any]]:
    _ = enable_native_file_blocks, supported_file_mime_types
    media_type = str(item.get("media_type") or "").strip().lower()
    path = str(item.get("path") or "").strip()
    url = str(item.get("url") or "").strip()
    name = str(item.get("name") or "").strip() or (Path(path).name if path else "attachment")
    mime = _guess_mime(path, str(item.get("mime_type") or ""))

    if media_type == "image":
        if not vision_supported:
            return None
        ms_ref = url if url and not _is_data_url(url) else None
        if not ms_ref and kimi_files is not None and path:
            ms_ref = kimi_files.ensure_ms_url(path=path, media_type="image")
            if ms_ref:
                item = {**item, "url": ms_ref, "vendor": "kimi"}
        if ms_ref:
            return {
                "type": "image_url",
                "image_url": {"url": ms_ref},
                "path": path,
                "name": name,
            }
        if path:
            data_url = _path_to_data_url(path, mime)
            if data_url:
                return {
                    "type": "image_url",
                    "image_url": {"url": data_url},
                    "path": path,
                    "name": name,
                }
        return None

    if media_type == "video":
        # 视频不在 messages 里直接挂载；仅保留 path 文字提示。
        return None

    if media_type == "file":
        # PDF/文档不在 messages 里直接挂载；由 read_file 等工具按 path 处理。
        return None

    return None


def _path_to_data_url(path: str, mime: str) -> Optional[str]:
    file_data = _read_path_as_base64(path)
    if not file_data:
        return None
    return f"data:{mime};base64,{file_data}"


def _hydrate_user_content_for_api(
    content: Any,
    *,
    vision_supported: bool,
    kimi_files: Optional["KimiVendorFilesClient"] = None,
) -> Any:
    """仅对图片 media_ref 做临时注入；PDF/视频不挂载二进制。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content

    hydrated: List[Dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "media_ref":
            api_item = _hydrate_media_ref_for_api(
                part,
                vision_supported=vision_supported,
                enable_native_file_blocks=False,
                supported_file_mime_types=set(),
                kimi_files=kimi_files,
            )
            if api_item is not None:
                hydrated.append(api_item)
            # file/video/PDF 的 media_ref 不传给 LLM API（路径已在 text preface 里）。
            continue
        if part.get("type") == "image_url":
            inner = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
            url = str(inner.get("url") or "").strip()
            path = str(part.get("path") or "").strip()
            if vision_supported and path and kimi_files is not None and _is_data_url(url):
                ms_ref = kimi_files.ensure_ms_url(path=path, media_type="image")
                if ms_ref:
                    hydrated.append(
                        {
                            **part,
                            "image_url": {"url": ms_ref},
                        }
                    )
                    continue
            if _is_data_url(url) and path and vision_supported:
                data_url = _path_to_data_url(path, _guess_mime(path, "image/png"))
                if data_url:
                    hydrated.append(
                        {
                            **part,
                            "image_url": {"url": data_url},
                        }
                    )
                continue
            if vision_supported:
                hydrated.append(part)
            continue
        if part.get("type") in ("video_url", "file") or (
            not vision_supported
            and part.get("type") not in ("text",)
        ):
            # 非 vision 模型 / 非 OpenAI content 类型：丢弃（路径信息应在 text preface 里）
            continue
        hydrated.append(part)
    if not hydrated:
        return ""
    return hydrated


def persist_kimi_ms_urls_in_media_items(
    items: List[Dict[str, Any]],
    *,
    kimi_files: Optional["KimiVendorFilesClient"],
) -> None:
    """将 ``ms://`` 写回 pending / content_items 中的 image media_ref。"""
    if kimi_files is None:
        return
    from agent_core.llm.vendor_files import is_kimi_ms_url

    for part in items:
        if not isinstance(part, dict) or part.get("type") != "media_ref":
            continue
        if str(part.get("media_type") or "").lower() != "image":
            continue
        if is_kimi_ms_url(str(part.get("url") or "")):
            continue
        path = str(part.get("path") or "").strip()
        if not path:
            continue
        ms_ref = kimi_files.ensure_ms_url(path=path, media_type="image")
        if ms_ref:
            part["url"] = ms_ref
            part["vendor"] = "kimi"


def persist_kimi_ms_urls_in_context(
    messages: List[Dict[str, Any]],
    *,
    kimi_files: Optional["KimiVendorFilesClient"],
) -> None:
    """
    上传成功后把 ``ms://`` 写回持久化 context 的 ``media_ref.url``，避免重复 upload。
    """
    if kimi_files is None:
        return
    from agent_core.llm.vendor_files import is_kimi_ms_url

    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "media_ref":
                continue
            if str(part.get("media_type") or "").lower() != "image":
                continue
            if is_kimi_ms_url(str(part.get("url") or "")):
                continue
            path = str(part.get("path") or "").strip()
            if not path:
                continue
            ms_ref = kimi_files.ensure_ms_url(path=path, media_type="image")
            if ms_ref:
                part["url"] = ms_ref
                part["vendor"] = "kimi"


def hydrate_messages_for_api(
    messages: List[Dict[str, Any]],
    *,
    current_turn_id: int,
    vision_supported: bool,
    enable_native_file_blocks: bool = False,
    supported_file_mime_types: Optional[List[str]] = None,
    kimi_files: Optional["KimiVendorFilesClient"] = None,
) -> List[Dict[str, Any]]:
    """
    组装 LLM 请求前处理 messages：

    - 剥离已持久化的 base64 / data URL；
    - 所有轮次用户消息中的图片：优先 ``ms://``（Kimi Files API），否则临时 base64；
    - PDF/视频/文档仅保留 path 文字。
    """
    _ = current_turn_id, enable_native_file_blocks, supported_file_mime_types
    out: List[Dict[str, Any]] = []
    for msg in messages:
        msg_copy = copy.deepcopy(msg)
        msg_copy.pop("_turn_id", None)
        content = msg_copy.get("content")
        if isinstance(content, list):
            msg_copy["content"] = _strip_binary_from_content_parts(content)
        if msg.get("role") == "user" and isinstance(msg_copy.get("content"), list):
            msg_copy["content"] = _hydrate_user_content_for_api(
                msg_copy["content"],
                vision_supported=vision_supported,
                kimi_files=kimi_files,
            )
        out.append(msg_copy)
    return out


def _strip_binary_from_content_parts(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove inline base64 from content parts; keep text and lightweight refs."""
    cleaned: List[Dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        t = part.get("type")
        if t == "file":
            path = str(part.get("path") or "").strip()
            if path:
                cleaned.append(
                    {
                        "type": "media_ref",
                        "media_type": "file",
                        "path": path,
                        "name": str(part.get("name") or "").strip(),
                        "mime_type": _guess_mime(
                            path, str(part.get("mime_type") or "")
                        ),
                    }
                )
            continue
        if t == "image_url":
            path = str(part.get("path") or "").strip()
            inner = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
            url = str(inner.get("url") or "").strip()
            if _is_data_url(url):
                if path:
                    cleaned.append(
                        {
                            "type": "media_ref",
                            "media_type": "image",
                            "path": path,
                            "name": str(part.get("name") or "").strip(),
                            "mime_type": _guess_mime(path, "image/png"),
                        }
                    )
                continue
            if url or path:
                cleaned.append(part)
            continue
        if t == "video_url":
            path = str(part.get("path") or "").strip()
            inner = part.get("video_url") if isinstance(part.get("video_url"), dict) else {}
            url = str(inner.get("url") or "").strip()
            if _is_data_url(url):
                if path:
                    cleaned.append(
                        {
                            "type": "media_ref",
                            "media_type": "video",
                            "path": path,
                            "name": str(part.get("name") or "").strip(),
                            "mime_type": _guess_mime(path, "video/mp4"),
                        }
                    )
                continue
            if url or path:
                cleaned.append(part)
            continue
        if t == "media_ref":
            cleaned.append(
                {
                    "type": "media_ref",
                    "media_type": str(part.get("media_type") or "file"),
                    "path": str(part.get("path") or "").strip(),
                    "url": str(part.get("url") or "").strip(),
                    "name": str(part.get("name") or "").strip(),
                    "mime_type": str(part.get("mime_type") or "").strip(),
                }
            )
            continue
        cleaned.append(part)
    return cleaned


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
        media_type = str(raw.get("media_type") or "").strip().lower()
        if t == "image_url" or (t == "media_ref" and media_type == "image"):
            seq_image += 1
            url = ""
            if t == "image_url":
                inner = raw.get("image_url")
                if isinstance(inner, dict):
                    url = str(inner.get("url") or "").strip()
            else:
                url = str(raw.get("url") or "").strip()
            name = str(raw.get("name") or "").strip() or f"{name_prefix}_{seq_image}"
            path = str(raw.get("path") or "").strip()
            remote_path = str(raw.get("remote_path") or "").strip()
            display_path = remote_path or path
            if unseen_media is not None:
                unseen_media.append(
                    {
                        "name": name,
                        "path": path,
                        "remote_path": remote_path,
                        "url": url,
                        "media_type": "image",
                    }
                )
            segs = [f"name={name}"]
            if display_path:
                segs.append(f"path={display_path}")
            lines.append(
                f"[用户附上图片 {' '.join(segs)}]，如需理解调用 recognize_image 工具"
            )
        elif t == "video_url" or (t == "media_ref" and media_type == "video"):
            seq_video += 1
            url = ""
            if t == "video_url":
                inner = raw.get("video_url")
                if isinstance(inner, dict):
                    url = str(inner.get("url") or "").strip()
            else:
                url = str(raw.get("url") or "").strip()
            name = str(raw.get("name") or "").strip() or f"video_{seq_video}"
            path = str(raw.get("path") or "").strip()
            remote_path = str(raw.get("remote_path") or "").strip()
            display_path = remote_path or path
            if unseen_media is not None:
                unseen_media.append(
                    {
                        "name": name,
                        "path": path,
                        "remote_path": remote_path,
                        "url": url,
                        "media_type": "video",
                    }
                )
            segs = [f"name={name}"]
            if display_path:
                segs.append(f"path={display_path}")
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
