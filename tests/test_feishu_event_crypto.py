"""飞书事件解密与签名辅助函数。"""

from __future__ import annotations

import json

import pytest

from frontend.feishu.event_crypto import decrypt_feishu_event_body, verify_feishu_http_signature


def test_decrypt_plain_passthrough() -> None:
    raw = {"header": {"event_type": "card.action.trigger"}, "event": {}}
    assert decrypt_feishu_event_body(raw, None) == raw


def test_decrypt_requires_key_when_encrypt_present() -> None:
    raw = {"encrypt": "dGVzdA=="}  # invalid cipher, just exercise branch
    with pytest.raises(ValueError, match="encrypt_key"):
        decrypt_feishu_event_body(raw, None)


def test_verify_skips_when_no_encrypt_key() -> None:
    class H:
        def get(self, _k: str, _d=None):
            return None

    assert verify_feishu_http_signature(
        body_bytes=b"{}",
        encrypt_key=None,
        headers=H(),
    )


def test_verify_with_bad_signature() -> None:
    class H:
        def __init__(self) -> None:
            self._d = {
                "X-Lark-Request-Timestamp": "1",
                "X-Lark-Request-Nonce": "2",
                "X-Lark-Signature": "wrong",
            }

        def get(self, k: str, _d=None):
            return self._d.get(k)

    ok = verify_feishu_http_signature(
        body_bytes=b'{"encrypt":"x"}',
        encrypt_key="test_encrypt_key_value",
        headers=H(),
    )
    assert ok is False
