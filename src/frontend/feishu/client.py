"""
飞书开放平台 HTTP 客户端封装。

- 发送文本消息、图片消息
- 下载消息中的资源文件（图片、视频、音频、文件）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

import json
import logging
import re
import httpx

from .config import get_feishu_config
from .markdown_filter import filter_markdown_for_feishu


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
            raise RuntimeError(
                "Feishu app_id/app_secret 未配置，无法获取 tenant_access_token"
            )

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
        # 统一在这里做 Markdown → 纯文本过滤，避免上层重复处理。
        safe_text = filter_markdown_for_feishu(text)

        token = await self._get_tenant_access_token()
        url = f"{self._base_url}/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": safe_text}, ensure_ascii=False),
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

    async def send_interactive_card(self, *, chat_id: str, card: Dict[str, Any]) -> str:
        """
        发送交互卡片（JSON 2.0）。

        content 为整卡对象序列化后的字符串，参见「发送消息内容」— 卡片 interactive。
        https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/im-v1/message/create_json

        Returns:
            成功时返回 message_id，便于后续 PATCH 更新同一条消息（见 patch_interactive_card_message）。
        """
        if not chat_id:
            raise ValueError("chat_id 不能为空")
        from .permission_card import interactive_content_string

        content_str = interactive_content_string(card)
        token = await self._get_tenant_access_token()
        url = f"{self._base_url}/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": content_str,
        }
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"发送飞书卡片失败: {data}")
        payload_data = data.get("data")
        mid = ""
        if isinstance(payload_data, dict):
            mid = str(payload_data.get("message_id") or "").strip()
        return mid

    async def patch_interactive_card_message(
        self, *, message_id: str, card: Dict[str, Any]
    ) -> None:
        """
        更新已发送的 interactive 卡片（整条替换为新的卡片 JSON）。

        https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/patch
        要求卡片 config 含 update_multi: true（与发送时一致）。
        """
        mid = (message_id or "").strip()
        if not mid:
            raise ValueError("message_id 不能为空")
        from .permission_card import interactive_content_string

        content_str = interactive_content_string(card)
        token = await self._get_tenant_access_token()
        url = f"{self._base_url}/open-apis/im/v1/messages/{mid}"
        payload = {"content": content_str}
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.patch(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"更新飞书卡片失败: {data}")

    async def create_cardkit_card_entity(self, *, card: Dict[str, Any]) -> str:
        """
        创建卡片实体（CardKit），用于流式更新等后续接口。

        POST /open-apis/cardkit/v1/cards — 需「创建与更新卡片 cardkit:card:write」。
        """
        data_str = json.dumps(card, ensure_ascii=False)
        token = await self._get_tenant_access_token()
        url = f"{self._base_url}/open-apis/cardkit/v1/cards"
        payload = {"type": "card_json", "data": data_str}
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"创建卡片实体失败: {data}")
        payload_data = data.get("data")
        if not isinstance(payload_data, dict):
            raise RuntimeError(f"创建卡片实体无 data: {data}")
        cid = str(payload_data.get("card_id") or "").strip()
        if not cid:
            raise RuntimeError(f"创建卡片实体未返回 card_id: {data}")
        return cid

    async def send_message_with_card_id(self, *, chat_id: str, card_id: str) -> str:
        """
        发送引用卡片实体的消息（content 为 type=card + card_id，与内嵌整卡 JSON 二选一）。

        返回 message_id（流式场景通常后续改卡走 card_id，不依赖 message_id）。
        """
        if not chat_id or not card_id:
            raise ValueError("chat_id/card_id 不能为空")
        content_obj = {"type": "card", "data": {"card_id": card_id}}
        content_str = json.dumps(content_obj, ensure_ascii=False)
        token = await self._get_tenant_access_token()
        url = f"{self._base_url}/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": content_str,
        }
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"发送卡片实体消息失败: {data}")
        payload_data = data.get("data")
        mid = ""
        if isinstance(payload_data, dict):
            mid = str(payload_data.get("message_id") or "").strip()
        return mid

    async def cardkit_put_streaming_text_content(
        self,
        *,
        card_id: str,
        element_id: str,
        content: str,
        sequence: int,
    ) -> None:
        """
        流式更新文本：传入当前全量 content；sequence 在同一张卡片上的所有 CardKit 操作中严格递增。

        PUT /open-apis/cardkit/v1/cards/:card_id/elements/:element_id/content
        """
        cid = (card_id or "").strip()
        eid = (element_id or "").strip()
        if not cid or not eid:
            raise ValueError("card_id/element_id 不能为空")
        token = await self._get_tenant_access_token()
        url = f"{self._base_url}/open-apis/cardkit/v1/cards/{cid}/elements/{eid}/content"
        body = {"content": content, "sequence": int(sequence)}
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.put(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"流式更新文本失败: {data}")

    async def cardkit_patch_card_settings(
        self,
        *,
        card_id: str,
        settings: Dict[str, Any],
        sequence: int,
    ) -> None:
        """PATCH /open-apis/cardkit/v1/cards/:card_id/settings（如关闭 streaming_mode）。"""
        cid = (card_id or "").strip()
        if not cid:
            raise ValueError("card_id 不能为空")
        settings_str = json.dumps(settings, ensure_ascii=False)
        token = await self._get_tenant_access_token()
        url = f"{self._base_url}/open-apis/cardkit/v1/cards/{cid}/settings"
        body = {"settings": settings_str, "sequence": int(sequence)}
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.patch(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"更新卡片配置失败: {data}")

    async def cardkit_replace_card_entity(
        self,
        *,
        card_id: str,
        card: Dict[str, Any],
        sequence: int,
    ) -> None:
        """
        全量更新卡片实体（含 header 标签、body 等）。

        PUT /open-apis/cardkit/v1/cards/:card_id
        流式场景在结束后应用此接口，才能把标题区 text_tag 从 Streaming 改为 Complete/Segment
        （仅 PATCH settings 不会更新 header）。
        """
        cid = (card_id or "").strip()
        if not cid:
            raise ValueError("card_id 不能为空")
        data_str = json.dumps(card, ensure_ascii=False)
        token = await self._get_tenant_access_token()
        url = f"{self._base_url}/open-apis/cardkit/v1/cards/{cid}"
        body = {
            "card": {"type": "card_json", "data": data_str},
            "sequence": int(sequence),
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.put(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"全量更新卡片实体失败: {data}")

    async def upload_image(
        self, *, image_bytes: bytes, content_type: str = "image/png"
    ) -> str:
        """
        上传图片并返回 image_key，用于发送图片消息。

        飞书接口: POST /open-apis/im/v1/images
        限制：图片不超过 10M，支持 JPEG/PNG/WEBP/GIF 等。
        """
        token = await self._get_tenant_access_token()
        url = f"{self._base_url}/open-apis/im/v1/images"
        headers = {"Authorization": f"Bearer {token}"}
        # 飞书要求 multipart: image_type=message, image=文件
        files = {"image": ("image", image_bytes, content_type)}
        data = {"image_type": "message"}
        async with httpx.AsyncClient(timeout=max(self._timeout, 30.0)) as client:
            resp = await client.post(url, headers=headers, data=data, files=files)
            resp.raise_for_status()
            result = resp.json()
        if int(result.get("code", 0)) != 0:
            raise RuntimeError(f"飞书上传图片失败: {result}")
        key = (result.get("data") or {}).get("image_key")
        if not key:
            raise RuntimeError(f"飞书上传图片未返回 image_key: {result}")
        return str(key)

    async def send_image_message(self, *, chat_id: str, image_key: str) -> None:
        """向指定 chat 发送图片消息（需先通过 upload_image 获得 image_key）。"""
        if not chat_id or not image_key:
            raise ValueError("chat_id 和 image_key 不能为空")
        token = await self._get_tenant_access_token()
        url = f"{self._base_url}/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}, ensure_ascii=False),
        }
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"发送飞书图片消息失败: {data}")

    async def send_reply_attachments(
        self,
        *,
        chat_id: str,
        attachments: List[Dict[str, Any]],
    ) -> None:
        """
        将 Agent 返回的附件列表中的图片上传并发送到指定会话。

        attachments 每项支持：
        - {"type": "image", "path": "..."} 或 {"type": "image", "url": "..."}
        - {"type": "file", "path": "..."} 或 {"type": "file", "url": "...", "file_name": "..."}
        """
        if not chat_id or not attachments:
            return
        for att in attachments:
            att_type = str(att.get("type") or "").strip().lower()
            if att_type == "image":
                image_bytes: Optional[bytes] = None
                content_type = "image/png"
                if "path" in att:
                    path = Path(att["path"]).expanduser().resolve()
                    if not path.exists() or not path.is_file():
                        continue
                    image_bytes = path.read_bytes()
                    suffix = path.suffix.lower()
                    if suffix in (".jpg", ".jpeg"):
                        content_type = "image/jpeg"
                    elif suffix == ".gif":
                        content_type = "image/gif"
                    elif suffix == ".webp":
                        content_type = "image/webp"
                elif "url" in att:
                    url_str = str(att["url"]).strip()
                    if url_str.startswith(("http://", "https://")):
                        async with httpx.AsyncClient(timeout=30.0) as client:
                            resp = await client.get(url_str)
                            resp.raise_for_status()
                            image_bytes = resp.content
                            ct = resp.headers.get("content-type", "")
                            if "image/" in ct:
                                content_type = ct.split(";")[0].strip()
                if not image_bytes or len(image_bytes) > 10 * 1024 * 1024:
                    continue
                try:
                    image_key = await self.upload_image(
                        image_bytes=image_bytes, content_type=content_type
                    )
                    await self.send_image_message(chat_id=chat_id, image_key=image_key)
                except Exception as exc:
                    logging.getLogger(__name__).warning("飞书发送回复附图失败: %s", exc)
                continue

            if att_type == "file":
                file_bytes: Optional[bytes] = None
                file_name = str(att.get("file_name") or "").strip()
                if "path" in att:
                    path = Path(att["path"]).expanduser().resolve()
                    if not path.exists() or not path.is_file():
                        continue
                    file_bytes = path.read_bytes()
                    if not file_name:
                        file_name = path.name
                elif "url" in att:
                    url_str = str(att["url"]).strip()
                    if url_str.startswith(("http://", "https://")):
                        async with httpx.AsyncClient(timeout=60.0) as client:
                            resp = await client.get(url_str)
                            resp.raise_for_status()
                            file_bytes = resp.content
                            if not file_name:
                                file_name = self._infer_filename_from_response(
                                    resp.headers.get("content-disposition"), url_str
                                )
                if not file_bytes:
                    continue
                file_name = file_name or "attachment.bin"
                try:
                    file_key = await self.upload_file(
                        file_bytes=file_bytes,
                        file_name=file_name,
                    )
                    await self.send_file_message(chat_id=chat_id, file_key=file_key)
                except Exception as exc:
                    logging.getLogger(__name__).warning("飞书发送回复附件失败: %s", exc)
                continue

    async def download_message_resource(
        self,
        *,
        message_id: str,
        file_key: str,
        resource_type: str,
    ) -> Tuple[bytes, str, str]:
        """
        下载消息中的资源文件（图片、视频、音频、文件）。

        飞书接口: GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}
        参考: https://open.feishu.cn/document/server-docs/im-v1/message-resource/get

        Args:
            message_id: 消息 ID
            file_key: 资源 key（图片用 image_key，文件/视频/音频用 file_key）
            resource_type: "image" 或 "file"

        Returns:
            (bytes, mime_type, filename) 或抛出 RuntimeError
        """
        if not message_id or not file_key:
            raise ValueError("message_id 和 file_key 不能为空")
        if resource_type not in ("image", "file"):
            resource_type = "file"

        token = await self._get_tenant_access_token()
        url = (
            f"{self._base_url}/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
            f"?type={resource_type}"
        )
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=max(self._timeout, 60.0)) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()

        content_type = resp.headers.get("content-type", "application/octet-stream")
        mime = content_type.split(";")[0].strip() or "application/octet-stream"
        filename = self._infer_filename_from_response(
            resp.headers.get("content-disposition")
        )
        return resp.content, mime, filename

    @staticmethod
    def _infer_filename_from_response(
        content_disposition: Optional[str],
        fallback_url: Optional[str] = None,
    ) -> str:
        if content_disposition:
            m = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.I)
            if m:
                return unquote(m.group(1)).strip('"').strip() or "attachment.bin"
            m = re.search(r'filename="?([^";]+)"?', content_disposition, re.I)
            if m:
                return m.group(1).strip() or "attachment.bin"
        if fallback_url:
            tail = (fallback_url.rsplit("/", 1)[-1] or "").split("?", 1)[0].strip()
            if tail:
                return tail
        return "attachment.bin"

    async def upload_file(self, *, file_bytes: bytes, file_name: str) -> str:
        """上传文件并返回 file_key，用于发送 file 消息。"""
        token = await self._get_tenant_access_token()
        url = f"{self._base_url}/open-apis/im/v1/files"
        headers = {"Authorization": f"Bearer {token}"}
        
        # 根据文件扩展名判断 MIME 类型
        _MIME_BY_EXT: Dict[str, str] = {
            ".pdf": "application/pdf",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".xls": "application/vnd.ms-excel",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".ppt": "application/vnd.ms-powerpoint",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".txt": "text/plain",
            ".csv": "text/csv",
            ".zip": "application/zip",
            ".rar": "application/x-rar-compressed",
            ".tar": "application/x-tar",
            ".gz": "application/gzip",
            ".json": "application/json",
            ".xml": "application/xml",
            ".html": "text/html",
            ".css": "text/css",
            ".js": "application/javascript",
            ".py": "text/x-python",
            ".tex": "application/x-tex",
        }
        p = Path(file_name)
        mime_type = _MIME_BY_EXT.get(p.suffix.lower(), "application/octet-stream")
        file_type = self._infer_upload_file_type(file_name)

        files = {
            "file": (
                file_name or "attachment.bin",
                file_bytes,
                mime_type,
            )
        }
        data = {
            "file_type": file_type,
            "file_name": file_name or "attachment.bin",
        }
        async with httpx.AsyncClient(timeout=max(self._timeout, 60.0)) as client:
            resp = await client.post(url, headers=headers, data=data, files=files)
            resp.raise_for_status()
            result = resp.json()
        if int(result.get("code", 0)) != 0:
            raise RuntimeError(f"飞书上传文件失败: {result}")
        key = (result.get("data") or {}).get("file_key")
        if not key:
            raise RuntimeError(f"飞书上传文件未返回 file_key: {result}")
        return str(key)

    @staticmethod
    def _infer_upload_file_type(file_name: str) -> str:
        suffix = Path(file_name or "").suffix.lower()
        if suffix == ".pdf":
            return "pdf"
        if suffix in {".doc", ".docx"}:
            return "doc"
        if suffix in {".xls", ".xlsx", ".csv"}:
            return "xls"
        if suffix in {".ppt", ".pptx"}:
            return "ppt"
        return "stream"

    async def send_file_message(self, *, chat_id: str, file_key: str) -> None:
        """向指定 chat 发送文件消息（需先通过 upload_file 获得 file_key）。"""
        if not chat_id or not file_key:
            raise ValueError("chat_id 和 file_key 不能为空")
        token = await self._get_tenant_access_token()
        url = f"{self._base_url}/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
        }
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        if int(data.get("code", 0)) != 0:
            raise RuntimeError(f"发送飞书文件消息失败: {data}")
