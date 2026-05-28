"""Dashboard authentication (file-based whitelist + optional env overrides)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import yaml
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from agent_core.config import find_config_file
from frontend.dashboard.paths import (
    CONSOLE_PREFIX,
    LOGIN_PATH,
    console_path,
    is_console_api,
    public_paths,
    public_prefixes,
)

logger = logging.getLogger(__name__)

SESSION_COOKIE = "macchiato_dashboard_session"
DEFAULT_AUTH_FILENAME = "dashboard_auth.yaml"


@dataclass(frozen=True)
class DashboardUser:
    username: str
    password: str


@dataclass(frozen=True)
class DashboardAuthConfig:
    enabled: bool
    users: tuple[DashboardUser, ...]
    auth_token: str
    secret: str
    session_ttl_seconds: int
    secure_cookies: bool

    @staticmethod
    def resolve_auth_path(*, config_dir: Path | None = None) -> Path:
        env_override = os.environ.get("MACCHIATO_DASHBOARD_AUTH_PATH", "").strip()
        if env_override:
            return Path(env_override).expanduser()
        base_dir = config_dir or find_config_file().parent
        return base_dir / DEFAULT_AUTH_FILENAME

    @classmethod
    def load(cls, *, config_dir: Path | None = None) -> "DashboardAuthConfig":
        auth_path = cls.resolve_auth_path(config_dir=config_dir)
        if auth_path.exists():
            try:
                return cls.from_yaml(auth_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load %s: %s", auth_path, exc)
        return cls.from_env()

    @classmethod
    def from_yaml(cls, raw_text: str) -> "DashboardAuthConfig":
        data = yaml.safe_load(raw_text) or {}
        if not isinstance(data, dict):
            raise ValueError("dashboard auth config must be a mapping")

        users = _parse_users(data.get("users"))
        auth_token = str(data.get("auth_token") or "").strip()
        explicit_enabled = data.get("enabled")
        enabled = bool(explicit_enabled) if explicit_enabled is not None else bool(users or auth_token)

        secret = str(data.get("session_secret") or data.get("secret") or "").strip()
        secret = _resolve_secret(secret, enabled=enabled)

        try:
            hours = float(data.get("session_hours", 168))
        except (TypeError, ValueError):
            hours = 168.0
        session_ttl_seconds = max(3600, int(hours * 3600))

        secure_cookies = _parse_bool(data.get("secure_cookies"))
        secure_cookies = secure_cookies or _env_secure_cookies()

        env_token = os.environ.get("MACCHIATO_DASHBOARD_AUTH_TOKEN", "").strip()
        if env_token:
            auth_token = env_token
        env_secret = os.environ.get("MACCHIATO_DASHBOARD_AUTH_SECRET", "").strip()
        if env_secret:
            secret = env_secret

        return cls(
            enabled=enabled,
            users=users,
            auth_token=auth_token,
            secret=secret,
            session_ttl_seconds=session_ttl_seconds,
            secure_cookies=secure_cookies,
        )

    @classmethod
    def from_env(cls) -> "DashboardAuthConfig":
        """Fallback when no dashboard_auth.yaml exists (local dev / legacy env)."""
        password = os.environ.get("MACCHIATO_DASHBOARD_PASSWORD", "").strip()
        auth_token = os.environ.get("MACCHIATO_DASHBOARD_AUTH_TOKEN", "").strip()
        username = os.environ.get("MACCHIATO_DASHBOARD_USERNAME", "admin").strip() or "admin"
        users: tuple[DashboardUser, ...] = ()
        if password:
            users = (DashboardUser(username=username, password=password),)
        enabled = bool(users or auth_token)
        secret = _resolve_secret(
            os.environ.get("MACCHIATO_DASHBOARD_AUTH_SECRET", "").strip(),
            enabled=enabled,
        )
        try:
            hours = float(os.environ.get("MACCHIATO_DASHBOARD_SESSION_HOURS", "168"))
        except ValueError:
            hours = 168.0
        session_ttl_seconds = max(3600, int(hours * 3600))
        return cls(
            enabled=enabled,
            users=users,
            auth_token=auth_token,
            secret=secret,
            session_ttl_seconds=session_ttl_seconds,
            secure_cookies=_env_secure_cookies(),
        )


def _parse_users(raw: Any) -> tuple[DashboardUser, ...]:
    if not raw:
        return ()
    users: list[DashboardUser] = []
    if isinstance(raw, dict):
        for username, password in raw.items():
            name = str(username or "").strip()
            pwd = str(password or "")
            if name and pwd:
                users.append(DashboardUser(username=name, password=pwd))
        return tuple(users)
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("username") or "").strip()
        pwd = str(item.get("password") or "")
        if name and pwd:
            users.append(DashboardUser(username=name, password=pwd))
    return tuple(users)


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_secure_cookies() -> bool:
    secure_raw = os.environ.get("MACCHIATO_DASHBOARD_SECURE_COOKIES", "").strip().lower()
    return secure_raw in {"1", "true", "yes", "on"}


def _resolve_secret(secret: str, *, enabled: bool) -> str:
    if secret:
        return secret
    if not enabled:
        return ""
    secret = secrets.token_hex(32)
    logger.warning(
        "Dashboard session secret not set; using ephemeral secret "
        "(sessions reset on restart). Set session_secret in dashboard_auth.yaml."
    )
    return secret


class DashboardAuth:
    """Verify credentials and issue signed session cookies."""

    def __init__(self, config: DashboardAuthConfig) -> None:
        self.config = config

    def status(self, *, authenticated: bool, subject: str = "") -> dict[str, Any]:
        return {
            "auth_required": self.config.enabled,
            "authenticated": authenticated if self.config.enabled else True,
            "username": subject or None,
        }

    def verify_credentials(self, username: str, password: str) -> Optional[str]:
        if not self.config.enabled:
            return username.strip() or "guest"
        user = (username or "").strip()
        pwd = password or ""
        if not user or not pwd:
            return None
        for entry in self.config.users:
            if secrets.compare_digest(user, entry.username) and secrets.compare_digest(
                pwd, entry.password
            ):
                return entry.username
        if self.config.auth_token and secrets.compare_digest(pwd, self.config.auth_token):
            return user or "token"
        return None

    def verify_bearer(self, token: str) -> bool:
        if not self.config.enabled:
            return True
        if not self.config.auth_token:
            return False
        return secrets.compare_digest((token or "").strip(), self.config.auth_token)

    def create_session_token(self, subject: str) -> str:
        payload = {
            "sub": subject,
            "exp": int(time.time()) + self.config.session_ttl_seconds,
        }
        body = (
            base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )
        sig = hmac.new(
            self.config.secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{body}.{sig}"

    def verify_session_token(self, token: str) -> Optional[str]:
        if not token or "." not in token:
            return None
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(
            self.config.secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not secrets.compare_digest(sig, expected):
            return None
        pad = "=" * (-len(body) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(body + pad))
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        exp = int(payload.get("exp") or 0)
        if exp <= int(time.time()):
            return None
        subject = str(payload.get("sub") or "").strip()
        if not subject:
            return None
        if self.config.users and not any(u.username == subject for u in self.config.users):
            if subject != "token":
                return None
        return subject

    def set_session_cookie(self, response: Response, subject: str) -> None:
        token = self.create_session_token(subject)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=self.config.secure_cookies,
            samesite="lax",
            max_age=self.config.session_ttl_seconds,
            path="/",
        )

    def clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")

    def authenticate_request(self, request: Request) -> Optional[str]:
        if not self.config.enabled:
            return "guest"
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            if self.verify_bearer(token):
                return "token"
        session = request.cookies.get(SESSION_COOKIE)
        if session:
            return self.verify_session_token(session)
        return None


class DashboardAuthMiddleware:
    """Protect dashboard routes when auth is enabled."""

    def __init__(self, app, auth: DashboardAuth) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        auth = self.auth
        path = request.url.path

        if auth.config.enabled:
            is_public = path in public_paths() or any(
                path.startswith(prefix) for prefix in public_prefixes()
            )
            if not is_public:
                subject = auth.authenticate_request(request)
                if subject:
                    request.state.dashboard_user = subject
                elif is_console_api(path):
                    response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
                    await response(scope, receive, send)
                    return
                else:
                    next_path = path if path.startswith(CONSOLE_PREFIX) else console_path("/")
                    response = RedirectResponse(
                        f"{LOGIN_PATH}?next={quote(next_path)}",
                        status_code=302,
                    )
                    await response(scope, receive, send)
                    return

        await self.app(scope, receive, send)
