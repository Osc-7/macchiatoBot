"""飞书消息资源 ContentResolver（前端集成层）。"""

from __future__ import annotations

import base64
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, Optional

from agent_core.content import ContentReference, ContentResolver, register_resolver
from agent_core.agent.workspace_paths import ensure_workspace_owner_layout
from agent_core.config import get_config

from .client import FeishuClient
from .config import get_feishu_config

logger = logging.getLogger(__name__)


class FeishuContentResolver(ContentResolver):
    """将飞书消息中的 image_key / file_key 解析为 LLM-ready content item。"""

    source = "feishu"

    def __init__(self, *, client: Optional[FeishuClient] = None) -> None:
        self._client = client

    def _get_client(self) -> FeishuClient:
        if self._client:
            return self._client
        cfg = get_feishu_config()
        return FeishuClient(timeout_seconds=max(cfg.timeout_seconds, 60.0))

    @staticmethod
    def _safe_filename(name: str) -> str:
        raw = (name or "").strip().replace("\x00", "")
        raw = Path(raw).name.replace("\\", "_").replace("/", "_")
        if not raw:
            return "attachment.bin"

        suffixes = "".join(Path(raw).suffixes)
        stem = raw[: -len(suffixes)] if suffixes else raw

        cleaned_stem = re.sub(r"[^\w .()\[\]-]+", "_", stem, flags=re.UNICODE)
        cleaned_stem = re.sub(r"\s+", "_", cleaned_stem).strip(" ._")
        cleaned_suffix = re.sub(r"[^A-Za-z0-9.]+", "", suffixes)

        if cleaned_suffix and not cleaned_suffix.startswith("."):
            cleaned_suffix = f".{cleaned_suffix.lstrip('.')}"
        if not cleaned_suffix:
            inferred_ext = Path(raw).suffix
            if inferred_ext:
                cleaned_suffix = inferred_ext

        return f"{cleaned_stem or 'attachment'}{cleaned_suffix}" or "attachment.bin"

    @staticmethod
    def _derive_filename(
        suggested: str,
        *,
        ref_type: str,
        key: str,
        mime: str,
    ) -> str:
        if suggested:
            return FeishuContentResolver._safe_filename(suggested)
        ext = mimetypes.guess_extension(mime or "") or ""
        if ref_type == "image" and not ext:
            ext = ".png"
        base = f"{ref_type}_{(key or 'file')[:12]}".strip("_")
        return FeishuContentResolver._safe_filename(base + ext)

    @staticmethod
    def _normalize_mime(mime: str, filename: str) -> str:
        mime_norm = str(mime or "").strip().lower()
        if mime_norm and mime_norm != "application/octet-stream":
            return mime_norm
        guessed, _ = mimetypes.guess_type(filename or "")
        return (guessed or mime_norm or "application/octet-stream").lower()

    @staticmethod
    def _workspace_upload_dir(source: str, user_id: str) -> Path:
        cfg = get_config()
        layout = ensure_workspace_owner_layout(
            cfg.command_tools,
            user_id or "unknown",
            source=source or "feishu",
        )
        d = Path(layout["owner_dir"]) / "uploads" / "feishu"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _extract_text_preview(file_path: Path, mime: str, limit: int = 12000) -> Optional[str]:
        suffix = file_path.suffix.lower()
        text_like_suffixes = {
            ".txt",
            ".md",
            ".markdown",
            ".json",
            ".csv",
            ".tsv",
            ".yaml",
            ".yml",
            ".xml",
            ".html",
            ".py",
            ".js",
            ".ts",
            ".java",
            ".go",
            ".rs",
            ".sql",
            ".log",
            ".ini",
        }
        is_text_mime = (mime or "").startswith("text/") or mime in {
            "application/json",
            "application/xml",
            "application/yaml",
        }
        if not is_text_mime and suffix not in text_like_suffixes:
            return None
        try:
            raw = file_path.read_bytes()
            return raw[:limit].decode("utf-8", errors="ignore").strip()
        except Exception:
            return None

    @staticmethod
    def _is_natively_supported_file_input(mime: str) -> bool:
        mime_norm = str(mime or "").strip().lower()
        return mime_norm == "application/pdf"

    async def resolve(self, ref: ContentReference) -> Optional[Dict[str, Any]]:
        extra = ref.extra or {}
        message_id = str(extra.get("message_id", "")).strip()
        if not message_id:
            logger.warning("feishu content ref missing message_id: key=%s", ref.key)
            return None

        resource_type = "image" if ref.ref_type == "image" else "file"
        try:
            client = self._get_client()
            raw_bytes, mime, file_name = await client.download_message_resource(
                message_id=message_id,
                file_key=ref.key,
                resource_type=resource_type,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "feishu download failed: message_id=%s file_key=%s: %s",
                message_id,
                ref.key,
                exc,
            )
            return None

        if not raw_bytes:
            return None

        source = str(extra.get("source") or "feishu").strip() or "feishu"
        user_id = str(extra.get("user_id") or "unknown").strip() or "unknown"
        uploads_dir = self._workspace_upload_dir(source, user_id)
        file_name_hint = str(extra.get("file_name") or "").strip()
        preferred_name = file_name_hint or file_name
        normalized_mime = self._normalize_mime(mime, preferred_name or file_name)

        resolved_name = self._derive_filename(
            preferred_name,
            ref_type=ref.ref_type,
            key=ref.key,
            mime=normalized_mime,
        )
        local_path = uploads_dir / resolved_name
        local_path.write_bytes(raw_bytes)

        data_url = (
            f"data:{normalized_mime};base64,"
            f"{base64.b64encode(raw_bytes).decode('ascii')}"
        )

        if ref.ref_type == "document":
            preview = self._extract_text_preview(local_path, normalized_mime)
            if self._is_natively_supported_file_input(normalized_mime):
                return {
                    "type": "user_file",
                    "file_data": base64.b64encode(raw_bytes).decode("ascii"),
                    "mime_type": normalized_mime,
                    "path": str(local_path),
                    "name": local_path.name,
                    "preview_text": preview or "",
                }

            content_lines = [
                f"[用户上传文件已保存到工作区] {local_path}",
                f"mime={normalized_mime}",
            ]
            if preview:
                content_lines.append("以下是文件内容预览：")
                content_lines.append(preview)
            else:
                content_lines.append("该文件类型暂不做内联解析，请按路径读取文件后再处理。")
            return {"type": "text", "text": "\n".join(content_lines)}

        if normalized_mime.startswith("video/"):
            return {
                "type": "video_url",
                "video_url": {"url": data_url},
                "path": str(local_path),
                "name": local_path.name,
                "defer_with_next_user_input": True,
            }
        if normalized_mime.startswith("audio/"):
            return {
                "type": "text",
                "text": (
                    f"[用户上传音频已保存到工作区] {local_path}\n"
                    "当前默认不做音频内联解析，请根据该路径选择后续处理。"
                ),
            }
        # image 及未知类型统一按图片处理
        return {
            "type": "image_url",
            "image_url": {"url": data_url},
            "path": str(local_path),
            "name": local_path.name,
            "defer_with_next_user_input": True,
        }


# 模块导入即向 agent_core 注册 FeishuContentResolver，供 resolve_content_refs 使用。
register_resolver(FeishuContentResolver())
