"""飞书消息资源 ContentResolver。"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, Optional

from frontend.feishu.client import FeishuClient
from frontend.feishu.config import get_feishu_config

from ..models import ContentReference
from ..resolver import ContentResolver

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

    async def resolve(self, ref: ContentReference) -> Optional[Dict[str, Any]]:
        extra = ref.extra or {}
        message_id = str(extra.get("message_id", "")).strip()
        if not message_id:
            logger.warning("feishu content ref missing message_id: key=%s", ref.key)
            return None

        resource_type = "image" if ref.ref_type == "image" else "file"
        try:
            client = self._get_client()
            raw_bytes, mime = await client.download_message_resource(
                message_id=message_id,
                file_key=ref.key,
                resource_type=resource_type,
            )
        except Exception as exc:
            logger.warning("feishu download failed: message_id=%s file_key=%s: %s", message_id, ref.key, exc)
            return None

        if not raw_bytes:
            return None

        data_url = f"data:{mime};base64,{base64.b64encode(raw_bytes).decode('ascii')}"

        if (mime or "").startswith("video/"):
            return {"type": "video_url", "video_url": {"url": data_url}}
        if (mime or "").startswith("audio/"):
            # 部分 LLM 支持 audio；若模型不支持则作为图片失败兜底，此处按音频注入
            # 当前主流多模态多为 image/video，audio 可能需单独处理
            return {"type": "image_url", "image_url": {"url": data_url}}
        # image 及未知类型统一按图片处理
        return {"type": "image_url", "image_url": {"url": data_url}}
