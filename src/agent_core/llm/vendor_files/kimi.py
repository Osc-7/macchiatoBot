"""Kimi / Moonshot Files API：上传一次，messages 里用 ms:// 引用。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

import httpx

logger = logging.getLogger(__name__)

_MS_PREFIX = "ms://"


def ms_url(file_id: str) -> str:
    fid = (file_id or "").strip()
    if fid.startswith(_MS_PREFIX):
        return fid
    return f"{_MS_PREFIX}{fid}"


def is_kimi_ms_url(url: str) -> bool:
    return str(url or "").strip().startswith(_MS_PREFIX)


def resolve_kimi_files_base_url(provider_base_url: str) -> Optional[str]:
    """
    从 active provider 的 base_url 推导 Files API 根路径。

    - Kimi Code: https://api.kimi.com/coding/v1
    - Moonshot:  https://api.moonshot.cn/v1 / https://api.moonshot.ai/v1
    """
    raw = (provider_base_url or "").strip().rstrip("/")
    if not raw:
        return None
    if "api.kimi.com/coding" in raw:
        return raw if raw.endswith("/v1") else f"{raw}/v1"
    if "moonshot.cn" in raw or "moonshot.ai" in raw:
        return raw if raw.endswith("/v1") else f"{raw}/v1"
    return None


def _cache_key(files_base_url: str, path: Path, purpose: str) -> str:
    try:
        st = path.stat()
        mtime_ns = st.st_mtime_ns
        size = st.st_size
    except OSError:
        mtime_ns = 0
        size = 0
    return f"{files_base_url}|{path.resolve()}|{purpose}|{mtime_ns}|{size}"


class KimiVendorFilesClient:
    """会话级缓存 + 同步上传到 Kimi Files API。"""

    def __init__(
        self,
        *,
        files_base_url: str,
        api_key: str,
        cache: Optional[Dict[str, str]] = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._files_base_url = files_base_url.rstrip("/")
        self._api_key = api_key.strip()
        self._cache = cache if cache is not None else {}
        self._timeout = timeout_seconds

    @property
    def files_base_url(self) -> str:
        return self._files_base_url

    def ensure_ms_url(
        self,
        *,
        path: str,
        media_type: str,
    ) -> Optional[str]:
        """
        确保本地图片/视频在 Kimi 侧有 file_id，并返回 ``ms://`` URL。

        文档/非媒体类型返回 None。
        """
        media = str(media_type or "").strip().lower()
        if media not in {"image", "video"}:
            return None
        p = Path(path)
        if not p.is_file():
            logger.warning("kimi files: local media missing path=%s", path)
            return None

        purpose = "image" if media == "image" else "video"
        key = _cache_key(self._files_base_url, p, purpose)
        cached = self._cache.get(key)
        if cached:
            return ms_url(cached)

        try:
            file_id = self._upload(path=p, purpose=purpose)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "kimi files upload failed path=%s purpose=%s: %s", path, purpose, exc
            )
            return None

        self._cache[key] = file_id
        return ms_url(file_id)

    def _upload(self, *, path: Path, purpose: str) -> str:
        url = f"{self._files_base_url}/files"
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": (path.name, path.read_bytes())},
                data={"purpose": purpose},
            )
            resp.raise_for_status()
            data = resp.json()
        file_id = str(data.get("id") or "").strip()
        if not file_id:
            raise ValueError(f"kimi files upload missing id: {data!r}")
        logger.info(
            "kimi files uploaded path=%s purpose=%s id=%s bytes=%s",
            path,
            purpose,
            file_id,
            data.get("bytes"),
        )
        return file_id


def build_kimi_vendor_files_client(
    *,
    provider_base_url: str,
    api_key: str,
    cache: Optional[Dict[str, str]] = None,
    timeout_seconds: float = 60.0,
) -> Optional[KimiVendorFilesClient]:
    files_base = resolve_kimi_files_base_url(provider_base_url)
    if not files_base or not api_key.strip():
        return None
    return KimiVendorFilesClient(
        files_base_url=files_base,
        api_key=api_key,
        cache=cache,
        timeout_seconds=timeout_seconds,
    )
