"""
水源社区 User-Api-Key 生成脚本。

通过浏览器授权获取 Discourse User-Api-Key，用于程序化访问水源社区 API。
参考：https://shuiyuan.sjtu.edu.cn/t/topic/123808
"""

import base64
import json
import secrets
import urllib.parse
import uuid
import webbrowser
from collections.abc import Iterable
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

SITE_URL_BASE = "https://shuiyuan.sjtu.edu.cn"
ALL_SCOPES = [
    "read",
    "write",
    "message_bus",
    "push",
    "one_time_password",
    "notifications",
    "session_info",
    "bookmarks_calendar",
    "user_status",
]
DEFAULT_SCOPES = ["read"]


@dataclass
class UserApiKeyPayload:
    key: str
    nonce: str
    push: bool
    api: int


@dataclass
class UserApiKeyRequestResult:
    client_id: str
    payload: UserApiKeyPayload


def generate_user_api_key(
    application_name: str,
    *,
    client_id: str | None = None,
    scopes: Iterable[str] | None = None,
    site_url: str | None = None,
) -> UserApiKeyRequestResult:
    """
    生成水源社区 User-Api-Key。

    会打开浏览器到水源社区授权页面，用户完成授权后需将返回的加密 payload 粘贴回终端。

    Args:
        application_name: 应用名称，用于在水源社区授权页展示
        client_id: 可选，客户端 ID；不传则自动生成 UUID
        scopes: 可选，权限范围；默认 ['read']，发帖需 ['read', 'write']
        site_url: 可选，水源社区站点 URL，默认 https://shuiyuan.sjtu.edu.cn

    Returns:
        UserApiKeyRequestResult，包含 client_id 和 payload.key（API Key）

    Raises:
        ValueError: scopes 无效或 nonce 校验失败
    """
    base = site_url or SITE_URL_BASE

    # Generate RSA key pair
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=4096,
    )
    public_key = private_key.public_key()
    public_key_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")

    client_id_to_use = str(uuid.uuid4()) if client_id is None else client_id
    nonce = secrets.token_urlsafe(32)

    scopes_list = DEFAULT_SCOPES if scopes is None else list(scopes)
    if not set(scopes_list) <= set(ALL_SCOPES):
        raise ValueError("Invalid scopes")

    params_dict: dict[str, str] = {
        "application_name": application_name,
        "client_id": client_id_to_use,
        "scopes": ",".join(scopes_list),
        "public_key": public_key_pem,
        "nonce": nonce,
    }
    params_str = "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in params_dict.items())
    webbrowser.open(f"{base}/user-api-key/new?{params_str}")

    print("在水源社区授权后，请粘贴返回的加密 payload（可多行），以空行结束:")
    lines: list[str] = []
    while True:
        line = input()
        if not line.strip():
            break
        lines.append(line.strip())
    enc_payload = "".join(lines)
    decrypted = private_key.decrypt(
        base64.b64decode(enc_payload),
        padding.PKCS1v15(),
    )
    dec_payload = UserApiKeyPayload(**json.loads(decrypted.decode("utf-8")))
    if dec_payload.nonce != nonce:
        raise ValueError("Nonce mismatch")

    return UserApiKeyRequestResult(
        client_id=client_id_to_use,
        payload=dec_payload,
    )


def main() -> None:
    """CLI 入口：生成 User-Api-Key 并测试。"""
    import requests

    result = generate_user_api_key("Shuiyuan Sample App")
    print(f"client_id: {result.client_id}")
    print(f"api_key: {result.payload.key}")
    print("\n正在测试...")
    r = requests.get(
        f"{SITE_URL_BASE}/search.json",
        params={"q": "tags:水源开发者"},
        headers={"User-Api-Key": result.payload.key},
        timeout=5,
    )
    print(r.json())


if __name__ == "__main__":
    main()
