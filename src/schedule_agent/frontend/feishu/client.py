from __future__ import annotations

"""
飞书开放平台 HTTP 客户端封装。

当前仅实现发送文本消息的最小能力；Token 获取与缓存逻辑根据
飞书官方文档（tenant_access_token internal 模式）实现。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import json
import httpx

from .config import get_feishu_config


@dataclass
class _TokenCache:
    token: str
    expire_at: datetime

    @property
    def is_valid(self) -> bool:
        # 预留 60 秒缓冲，避免临界点过期
        return datetime.now(timezone.utc) < self.expire_at - timedelta(seconds=60)


class FeishuClient:
    """飞书 API 客户端（最小实现：获取 tenant_access_token + 发送文本消息）。"""

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        cfg = get_feishu_config()
        self._cfg = cfg
        self._base_url = cfg.base_url.rstrip("/")
        self._timeout = timeout_seconds or cfg.timeout_seconds
        self._tenant_token_cache: Optional[_TokenCache] = None

    async def _get_tenant_access_token(self) -> str:
        """获取（或复用缓存的）tenant_access_token。"""
        if self._tenant_token_cache and self._tenant_token_cache.is_valid:
            return self._tenant_token_cache.token

        if not (self._cfg.app_id and self._cfg.app_secret):
            raise RuntimeError("Feishu app_id/app_secret 未配置，无法获取 tenant_access_token")

        url = f"{self._base_url}/open-apis/auth/v3/tenant_access_token/internal"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                url,
                json={"app_id": self._cfg.app_id, "app_secret": self._cfg.app_secret},
            )
            resp.raise_for_status()
            data = resp.json()
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
        token = str(data.get("tenant_access_token") or "")
        expire = int(data.get("expire", 0) or data.get("expire_in", 0) or 3600)
        self._tenant_token_cache = _TokenCache(
            token=token,
            expire_at=datetime.now(timezone.utc) + timedelta(seconds=expire),
        )
        return token

    async def send_text_message(
        self,
        *,
        chat_id: str,
        text: str,
    ) -> None:
        """
        向指定 chat 发送纯文本消息。

        Args:
            chat_id: 飞书会话 chat_id
            text: 文本内容
        """
        if not chat_id:
            raise ValueError("chat_id 不能为空")
        token = await self._get_tenant_access_token()
        url = f"{self._base_url}/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        headers = {
            "Authorization": f"Bearer {token}",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        if int(data.get("code", 0)) != 0:
            # 失败时抛出异常，由上层记录日志并向用户返回友好错误
            raise RuntimeError(f"发送飞书消息失败: {data}")

