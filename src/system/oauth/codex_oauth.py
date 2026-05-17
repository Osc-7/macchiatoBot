"""
OpenAI Codex OAuth 认证流程（PKCE）。

基于公开的 OpenAI Codex CLI OAuth 端点实现。
redirect_uri 硬编码为 http://localhost:1455/auth/callback（不可配置），
采用 copy-paste 方案：用户在浏览器完成授权后将回调 URL 粘贴回来，
从中提取 authorization code 完成 token 交换。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---- OpenAI Codex OAuth 公开常量（来自开源代码） ----
AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"

# ---- 常量 ----
DEFAULT_AUTH_DIR = Path("data/oauth")


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_pkce_pair() -> tuple[str, str]:
    """生成 PKCE code_verifier（32 字节随机）和 SHA-256 code_challenge。"""
    code_verifier = _base64url(secrets.token_bytes(32))
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = _base64url(digest)
    return code_verifier, code_challenge


def build_auth_url(code_challenge: str, state: str) -> str:
    """构建浏览器授权 URL。"""
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "pi",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def parse_callback_url(callback_url: str) -> Optional[str]:
    """从回调 URL 中提取 authorization code。

    用户粘贴的 URL 形如：
    http://localhost:1455/auth/callback?code=xxx&state=yyy&scope=...

    Returns:
        authorization code 字符串，解析失败返回 None。
    """
    try:
        parsed = urllib.parse.urlparse(callback_url)
        params = urllib.parse.parse_qs(parsed.query)
        codes = params.get("code", [])
        if codes:
            return codes[0]
    except Exception:
        pass
    return None


async def exchange_code_for_token(code: str, code_verifier: str) -> dict:
    """用 authorization code 交换 token 对。

    Returns:
        {
            "access_token": "...",
            "refresh_token": "...",
            "expires_in": 86400,
            "token_type": "Bearer",
            "scope": "openid profile email offline_access",
        }

    Raises:
        httpx.HTTPStatusError: 交换失败（code 过期、verifier 不匹配等）。
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info("Token exchange succeeded, expires_in=%s", data.get("expires_in"))
        return data


async def refresh_access_token(refresh_token: str) -> dict:
    """用 refresh_token 刷新 access_token。

    Returns:
        与 exchange_code_for_token 相同的格式，但通常不含新的 refresh_token。

    Raises:
        httpx.HTTPStatusError: 刷新失败（refresh_token 已过期或 revoked）。
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info("Token refresh succeeded, expires_in=%s", data.get("expires_in"))
        return data


@dataclass
class TokenState:
    access_token: str
    refresh_token: str
    expires_at: float  # Unix timestamp


def save_token_state(file_path: Path, state: TokenState) -> None:
    """加密存储 token 到文件（当前用 JSON 明文，后续可上 encryption）。"""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "access_token": state.access_token,
        "refresh_token": state.refresh_token,
        "expires_at": state.expires_at,
    }
    file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    file_path.chmod(0o600)
    logger.info("Token saved to %s", file_path)


def load_token_state(file_path: Path) -> Optional[TokenState]:
    """从文件加载 token 状态。"""
    if not file_path.exists():
        return None
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return TokenState(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=data["expires_at"],
        )
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning("Failed to load token from %s: %s", file_path, e)
        return None
