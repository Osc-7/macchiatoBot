"""
水源社区图片 ContentResolver。

将 upload:// 短链（已由 content_parser 转换为 HTTPS URL）带 User-Api-Key
下载到本地缓存文件，返回 media_ref{path}；hydrate 阶段再由
media_helpers 走 path → base64 data URL 分支注入 LLM 请求。

注意：不能把水源的 HTTPS URL 直接透传给 LLM provider（DeepSeek 等），
provider 服务端裸拉水源图床拿不到内容（需要登录态 / User-Api-Key），
会报 "Failed to download image"。因此必须把字节落在本地。

模块导入时自动向 agent_core 注册，无需手动调用。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
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


_EXT_BY_MIME: Dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    d = Path(base) / "macchiato" / "shuiyuan-media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_to_cache(url: str, raw: bytes, mime: str) -> Optional[Path]:
    """把下载的字节落盘为本地缓存文件（按 URL 哈希去重），失败返回 None。"""
    try:
        ext = _EXT_BY_MIME.get(mime) or (
            Path(url.rsplit("?", 1)[0]).suffix.lower() or ".jpg"
        )
        name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:24] + ext
        p = _cache_dir() / name
        if not p.exists() or p.stat().st_size != len(raw):
            p.write_bytes(raw)
        return p
    except Exception as exc:
        logger.warning("shuiyuan_resolver: cache write failed url=%s: %s", url, exc)
        return None


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

        if (mime or "").startswith("video/"):
            return {
                "type": "media_ref",
                "media_type": "video",
                "url": url,
                "mime_type": mime,
            }
        local_path = _save_to_cache(url, raw_bytes, mime)
        if local_path is not None:
            return {
                "type": "media_ref",
                "media_type": "image",
                "path": str(local_path),
                "mime_type": mime,
            }
        # 缓存写失败时回退为 URL 透传（provider 多半拉不到，但聊胜于无）
        return {
            "type": "media_ref",
            "media_type": "image",
            "url": url,
            "mime_type": mime,
        }


# 模块导入时即向 agent_core 注册，供 resolve_content_refs 使用。
register_resolver(ShuiyuanContentResolver())
