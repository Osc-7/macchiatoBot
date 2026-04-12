"""飞书事件 HTTP 回调：解密与签名校验（与 lark_oapi EventDispatcherHandler 行为对齐）。"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from lark_oapi.core.utils.decryptor import AESCipher

logger = logging.getLogger(__name__)

# 与 lark_oapi.core.const 一致
LARK_REQUEST_TIMESTAMP = "X-Lark-Request-Timestamp"
LARK_REQUEST_NONCE = "X-Lark-Request-Nonce"
LARK_REQUEST_SIGNATURE = "X-Lark-Signature"


def decrypt_feishu_event_body(
    raw: Dict[str, Any], encrypt_key: Optional[str]
) -> Dict[str, Any]:
    """
    若 body 含 ``encrypt`` 字段，则用 Encrypt Key 解密为明文 JSON（与长连接 SDK 相同算法）。
    否则原样返回。
    """
    enc = raw.get("encrypt")
    if not enc:
        return raw
    if not encrypt_key or not str(encrypt_key).strip():
        raise ValueError(
            "飞书回调 payload 已加密（含 encrypt 字段），请在 config / 环境变量中配置 feishu.encrypt_key"
        )
    plain = AESCipher(encrypt_key).decrypt_str(str(enc))
    return json.loads(plain)


def verify_feishu_http_signature(
    *,
    body_bytes: bytes,
    encrypt_key: Optional[str],
    headers: Any,
) -> bool:
    """
    校验 ``X-Lark-Signature``（与 lark_oapi EventDispatcherHandler._verify_sign 一致）。

    若请求未带签名头（明文、未启用加密时常见），则跳过校验并返回 True。
    """
    if not encrypt_key or not str(encrypt_key).strip():
        return True
    timestamp = headers.get(LARK_REQUEST_TIMESTAMP)
    nonce = headers.get(LARK_REQUEST_NONCE)
    signature = headers.get(LARK_REQUEST_SIGNATURE)
    if not (timestamp and nonce and signature):
        return True
    bs = (str(timestamp) + str(nonce) + str(encrypt_key)).encode("utf-8") + body_bytes
    expected = hashlib.sha256(bs).hexdigest()
    ok = str(signature) == expected
    if not ok:
        logger.warning(
            "feishu HTTP 签名校验失败（请检查 encrypt_key 与开放平台配置是否一致）"
        )
    return ok
