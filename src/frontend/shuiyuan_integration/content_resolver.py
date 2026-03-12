"""
水源社区图片 ContentResolver。

将 upload:// 短链（已由 content_parser 转换为 HTTPS URL）下载为
base64 data URL，供 LLM 多模态推理使用。

模块导入时自动向 agent_core 注册，无需手动调用。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Any, Dict, Optional

import requests

from agent_core.content import ContentReference, ContentResolver, register_resolver

logger = logging.getLogger(__name__)

_MIME_BY_EXT: Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _guess_mime(url: str) -> str:
    lower = url.lower().rsplit("?", 1)[0]
    for ext, mime in _MIME_BY_EXT.items():
        if lower.endswith(ext):
            return mime
    return "image/png"


def _get_api_key() -> str:
    """从配置或环境变量中获取 User-Api-Key。"""
    try:
        from agent_core.config import get_config

        cfg = get_config()
        if cfg.shuiyuan.enabled:
            keys = getattr(cfg.shuiyuan, "user_api_keys", None) or []
            if keys:
                return str(keys[0]).strip()
            single = getattr(cfg.shuiyuan, "user_api_key", None) or ""
            return str(single).strip()
    except Exception:
        pass
    return os.environ.get("SHUIYUAN_USER_API_KEY", "")


class ShuiyuanContentResolver(ContentResolver):
    """
    将水源社区图片 URL（已从 upload:// 转换为 HTTPS）下载为 base64 data URL。

    下载时使用 User-Api-Key 认证，确保在非公开话题中也能取到图片。
    """

    source = "shuiyuan"

    def __init__(self, *, timeout: float = 20.0) -> None:
        self._timeout = timeout

    async def resolve(self, ref: ContentReference) -> Optional[Dict[str, Any]]:
        url = ref.key
        if not url.startswith("http"):
            return None

        api_key = _get_api_key()
        headers: Dict[str, str] = {}
        if api_key:
            headers["User-Api-Key"] = api_key

        try:
            logger.info("shuiyuan_resolver: downloading url=%s", url)
            resp = await asyncio.to_thread(
                requests.get, url, headers=headers, timeout=self._timeout
            )
            resp.raise_for_status()
            raw_bytes = resp.content
            ct = resp.headers.get("Content-Type", "").split(";")[0].strip()
            mime = ct if ct and ct != "application/octet-stream" else _guess_mime(url)
        except Exception as exc:
            logger.warning("shuiyuan content resolver failed url=%s: %s", url, exc)
            return None

        logger.info(
            "shuiyuan_resolver: downloaded url=%s bytes=%d mime=%s",
            url,
            len(raw_bytes),
            mime,
        )

        data_url = (
            f"data:{mime};base64,{base64.b64encode(raw_bytes).decode('ascii')}"
        )
        if (mime or "").startswith("video/"):
            return {"type": "video_url", "video_url": {"url": data_url}}
        return {"type": "image_url", "image_url": {"url": data_url}}


# 模块导入时即向 agent_core 注册，供 resolve_content_refs 使用。
register_resolver(ShuiyuanContentResolver())
