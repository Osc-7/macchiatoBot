"""WebSocket server for local macchiato-remote workers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import re
import secrets
import time
from threading import RLock
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from fastapi.responses import HTMLResponse

from agent_core.config import get_config
from agent_core.remote.worker_registry import (RemoteWorkerConnection,
                                               get_remote_worker_registry)
from frontend.feishu.client import FeishuClient
from frontend.feishu.remote_login_card import (APPROVE, REJECT,
                                               build_remote_login_request_card)
from macchiato_remote.tokens import (expected_token_matches,
                                     load_registered_remote_worker_tokens,
                                     register_remote_worker_token,
                                     remote_token_registry_path)

logger = logging.getLogger(__name__)

# 默认 WebSocket 端口（避免与常见 8765 开发端口冲突）；可用 MACCHIATO_REMOTE_PORT 覆盖。
DEFAULT_REMOTE_WORKER_WEBSOCKET_PORT = 9380
_REMOTE_LOGIN_DEVICE_TTL_SECONDS = 600
_REMOTE_LOGIN_POLL_INTERVAL_SECONDS = 2
_REMOTE_LOGIN_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$")

_remote_login_lock = RLock()
_remote_login_pending: dict[str, dict[str, object]] = {}


class RemoteLoginStartRequest(BaseModel):
    login: str = Field(..., description="worker login alias")
    device_name: str = Field(default="", description="optional device name")
    bootstrap_token: str = Field(
        default="",
        description="one-time bootstrap auth token for first login exchange",
    )


class RemoteLoginPollRequest(BaseModel):
    device_code: str = Field(..., description="opaque device code")


class RemoteLoginApproveRequest(BaseModel):
    user_code: str = Field(..., description="human-friendly approval code")
    approver_secret: str = Field(..., description="server-side approver secret")
    approve: bool = Field(default=True, description="approve or deny")


def remote_server_enabled() -> bool:
    raw = os.environ.get("MACCHIATO_REMOTE_SERVER_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def remote_login_approver_secret() -> str:
    return os.environ.get("MACCHIATO_REMOTE_LOGIN_APPROVER_SECRET", "").strip()


def remote_login_bootstrap_token() -> str:
    return os.environ.get("MACCHIATO_REMOTE_LOGIN_BOOTSTRAP_TOKEN", "").strip()


def remote_login_approval_chat_id() -> str:
    env_chat = os.environ.get("MACCHIATO_REMOTE_LOGIN_FEISHU_CHAT_ID", "").strip()
    if env_chat:
        return env_chat
    cfg = get_config().feishu
    return str(getattr(cfg, "automation_activity_chat_id", "") or "").strip()


def remote_login_allowed_approver_open_ids() -> set[str]:
    raw = os.environ.get("MACCHIATO_REMOTE_LOGIN_APPROVER_OPEN_IDS", "").strip()
    if raw:
        return {part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()}
    cfg = get_config().feishu
    return {
        str(v).strip()
        for v in getattr(cfg, "remote_login_approver_open_ids", []) or []
        if str(v).strip()
    }


def remote_login_allowed_approver_user_ids() -> set[str]:
    raw = os.environ.get("MACCHIATO_REMOTE_LOGIN_APPROVER_USER_IDS", "").strip()
    if raw:
        return {part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()}
    cfg = get_config().feishu
    return {
        str(v).strip()
        for v in getattr(cfg, "remote_login_approver_user_ids", []) or []
        if str(v).strip()
    }


def remote_login_allowed_logins() -> set[str]:
    raw = os.environ.get("MACCHIATO_REMOTE_LOGIN_ALLOWED_LOGINS", "").strip()
    if not raw:
        return set()
    return {part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()}


def _remote_login_enabled() -> bool:
    return bool(
        remote_login_bootstrap_token()
        or remote_login_approver_secret()
        or _remote_login_feishu_enabled()
    )


def _remote_login_feishu_enabled() -> bool:
    cfg = get_config().feishu
    return bool(getattr(cfg, "enabled", False) and remote_login_approval_chat_id())


def _normalize_login_alias(raw: str) -> str:
    return (raw or "").strip()


def _is_login_alias_allowed(login: str) -> bool:
    if not _REMOTE_LOGIN_ALIAS_RE.match(login):
        return False
    allow = remote_login_allowed_logins()
    return not allow or login in allow


def _generate_user_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    token = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{token[:4]}-{token[4:]}"


def _remote_login_cleanup_expired(now_ts: Optional[float] = None) -> None:
    now = float(now_ts or time.time())
    expired: list[str] = []
    with _remote_login_lock:
        for device_code, record in _remote_login_pending.items():
            expires_at = float(record.get("expires_at") or 0)
            if expires_at <= now:
                expired.append(device_code)
        for code in expired:
            _remote_login_pending.pop(code, None)


def clear_remote_login_state_for_tests() -> None:
    with _remote_login_lock:
        _remote_login_pending.clear()


def _iso_now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _is_remote_login_approver_allowed(*, open_id: str, user_id: str) -> bool:
    allow_open_ids = remote_login_allowed_approver_open_ids()
    allow_user_ids = remote_login_allowed_approver_user_ids()
    if not allow_open_ids and not allow_user_ids:
        return False
    if open_id and open_id in allow_open_ids:
        return True
    if user_id and user_id in allow_user_ids:
        return True
    return False


async def _send_remote_login_approval_card(record: dict[str, object]) -> tuple[bool, str]:
    chat_id = remote_login_approval_chat_id()
    if not chat_id:
        return False, "missing_approval_chat_id"
    card = build_remote_login_request_card(
        request_id=str(record.get("device_code") or ""),
        login=str(record.get("login") or ""),
        device_name=str(record.get("device_name") or ""),
        requester_ip=str(record.get("requester_ip") or ""),
        created_at=str(record.get("created_at_iso") or ""),
    )
    try:
        client = FeishuClient(timeout_seconds=10.0)
        message_id = await client.send_interactive_card(chat_id=chat_id, card=card)
    except Exception as exc:  # noqa: BLE001
        logger.warning("remote login approval card send failed: %s", exc)
        return False, str(exc) or "send_failed"
    with _remote_login_lock:
        code = str(record.get("device_code") or "")
        existing = _remote_login_pending.get(code)
        if existing is not None:
            existing["approval_card_message_id"] = message_id
            _remote_login_pending[code] = existing
    return True, ""


def resolve_remote_login_request_from_feishu(
    *,
    request_id: str,
    approve: bool,
    approver_open_id: str = "",
    approver_user_id: str = "",
) -> tuple[str, str, Optional[dict[str, object]]]:
    rid = (request_id or "").strip()
    if not rid:
        return "warning", "缺少 request_id", None
    if not _is_remote_login_approver_allowed(
        open_id=(approver_open_id or "").strip(),
        user_id=(approver_user_id or "").strip(),
    ):
        return "error", "你没有远程登录审批权限", None
    now = time.time()
    _remote_login_cleanup_expired(now)
    with _remote_login_lock:
        record = _remote_login_pending.get(rid)
        if record is None:
            return "warning", "该登录请求已处理或已过期", None
        status = str(record.get("status") or "pending")
        if status != "pending":
            return "warning", "该登录请求已处理", None
        if not approve:
            record["status"] = "denied"
            record["approved_by_open_id"] = (approver_open_id or "").strip()
            record["approved_by_user_id"] = (approver_user_id or "").strip()
            _remote_login_pending[rid] = record
            card = build_remote_login_request_card(
                request_id=rid,
                login=str(record.get("login") or ""),
                device_name=str(record.get("device_name") or ""),
                requester_ip=str(record.get("requester_ip") or ""),
                created_at=str(record.get("created_at_iso") or ""),
                resolved=REJECT,
                approver_label=(approver_open_id or approver_user_id or "").strip(),
            )
            return "success", "已拒绝该远程登录请求", card
        login = str(record.get("login") or "").strip()
        token_value = secrets.token_urlsafe(32)
        register_remote_worker_token(login, token_value)
        record["status"] = "approved"
        record["approved_by_open_id"] = (approver_open_id or "").strip()
        record["approved_by_user_id"] = (approver_user_id or "").strip()
        record["approved_at"] = now
        record["token"] = token_value
        _remote_login_pending[rid] = record
        card = build_remote_login_request_card(
            request_id=rid,
            login=login,
            device_name=str(record.get("device_name") or ""),
            requester_ip=str(record.get("requester_ip") or ""),
            created_at=str(record.get("created_at_iso") or ""),
            resolved=APPROVE,
            approver_label=(approver_open_id or approver_user_id or "").strip(),
        )
        return "success", "已批准该远程登录请求", card


def remote_server_host() -> str:
    # 默认可从其他机器连接；仅内网或已配 TLS/防火墙时使用 127.0.0.1
    return os.environ.get("MACCHIATO_REMOTE_HOST", "0.0.0.0").strip() or "0.0.0.0"


def remote_server_port() -> int:
    raw = os.environ.get(
        "MACCHIATO_REMOTE_PORT", str(DEFAULT_REMOTE_WORKER_WEBSOCKET_PORT)
    ).strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_REMOTE_WORKER_WEBSOCKET_PORT


def remote_server_token() -> Optional[str]:
    token = os.environ.get("MACCHIATO_REMOTE_TOKEN", "").strip()
    return token or None


def remote_server_token_map() -> dict[str, str]:
    """Return login-specific worker tokens from registry file and env.

    ``macchiato-remote gen-token --login`` writes a hashed token registry to
    ``data/automation/remote_worker_tokens.json`` by default. The env var remains
    useful for container/runtime overrides and wins over the registry when both
    define the same login.

    Accepted ``MACCHIATO_REMOTE_TOKENS`` formats:
    - ``work-mbp=tok1,home-mini=tok2``
    - one entry per line
    - JSON object, e.g. ``{"work-mbp": "tok1"}``
    """
    out = load_registered_remote_worker_tokens()
    raw = os.environ.get("MACCHIATO_REMOTE_TOKENS", "").strip()
    if not raw:
        return out
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("MACCHIATO_REMOTE_TOKENS JSON parse failed; ignoring")
            return out
        if not isinstance(data, dict):
            return out
        out.update(
            {
                str(k).strip(): str(v).strip()
                for k, v in data.items()
                if str(k).strip() and str(v).strip()
            }
        )
        return out

    normalized = raw.replace("\n", ",").replace(";", ",")
    for item in normalized.split(","):
        entry = item.strip()
        if not entry:
            continue
        sep = "=" if "=" in entry else ":"
        if sep not in entry:
            logger.warning("invalid MACCHIATO_REMOTE_TOKENS entry ignored: %s", entry)
            continue
        login, tok = entry.split(sep, 1)
        login_s = login.strip()
        tok_s = tok.strip()
        if login_s and tok_s:
            out[login_s] = tok_s
    return out


def verify_remote_worker_token(
    *,
    login: str,
    supplied_token: str,
    token_override: Optional[str] = None,
    token_map: Optional[dict[str, str]] = None,
) -> tuple[bool, str]:
    """Validate a worker token.

    Per-login tokens win. ``MACCHIATO_REMOTE_TOKEN`` remains a compatibility
    fallback. If only per-login tokens are configured, unknown logins are
    rejected instead of silently becoming unauthenticated.
    """
    login_s = (login or "").strip()
    supplied = (supplied_token or "").strip()
    tokens = token_map if token_map is not None else remote_server_token_map()
    fallback = (token_override or remote_server_token() or "").strip()

    expected = tokens.get(login_s)
    if expected is None and fallback:
        expected = fallback
    if expected is None and tokens:
        return False, "unknown_login"
    if expected is None:
        return True, "auth_disabled"
    if expected_token_matches(supplied, expected):
        return True, "ok"
    return False, "token_mismatch"


def create_remote_worker_app(*, token: Optional[str] = None) -> FastAPI:
    if not (token or remote_server_token() or remote_server_token_map()):
        logger.warning(
            "No remote worker token configured: registry=%s, "
            "MACCHIATO_REMOTE_TOKEN/MACCHIATO_REMOTE_TOKENS unset. "
            "Run `macchiato-remote gen-token --login <name>` on the server.",
            remote_token_registry_path(),
        )
    app = FastAPI(title="macchiato remote worker gateway")

    @app.get("/", response_class=HTMLResponse)
    async def root_page() -> str:
        return _login_panel_html()

    @app.get("/remote/login", response_class=HTMLResponse)
    async def login_panel_page() -> str:
        return _login_panel_html()

    @app.get("/remote/healthz")
    async def remote_healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/remote/login/start")
    async def remote_login_start(
        req: RemoteLoginStartRequest, request: Request
    ) -> dict[str, object]:
        if not _remote_login_enabled():
            return {
                "ok": False,
                "error": "login_panel_disabled",
                "message": (
                    "Remote login panel is disabled. Set "
                    "MACCHIATO_REMOTE_LOGIN_BOOTSTRAP_TOKEN, or configure "
                    "Feishu approval card channel."
                ),
            }
        login = _normalize_login_alias(req.login)
        if not _is_login_alias_allowed(login):
            return {
                "ok": False,
                "error": "login_not_allowed",
                "message": (
                    "Login alias is not allowed. Check alias format or "
                    "MACCHIATO_REMOTE_LOGIN_ALLOWED_LOGINS."
                ),
            }
        bootstrap_expected = remote_login_bootstrap_token()
        has_feishu_approval = _remote_login_feishu_enabled()
        has_panel_approval = bool(remote_login_approver_secret())
        if bootstrap_expected:
            supplied_bootstrap = (req.bootstrap_token or "").strip()
            if supplied_bootstrap:
                if not secrets.compare_digest(supplied_bootstrap, bootstrap_expected):
                    return {
                        "ok": False,
                        "error": "forbidden",
                        "message": "Invalid bootstrap token.",
                    }
                token_value = secrets.token_urlsafe(32)
                register_remote_worker_token(login, token_value)
                return {
                    "ok": True,
                    "status": "approved",
                    "login": login,
                    "token": token_value,
                    "mode": "bootstrap_exchange",
                }
            if not (has_feishu_approval or has_panel_approval):
                return {
                    "ok": False,
                    "error": "bootstrap_token_required",
                    "message": "Missing bootstrap token for first login exchange.",
                }
        now = time.time()
        _remote_login_cleanup_expired(now)
        device_code = secrets.token_urlsafe(24)
        user_code = _generate_user_code()
        requester_ip = ""
        if request.client is not None:
            requester_ip = str(request.client.host or "").strip()
        created_at_iso = _iso_now_utc()
        with _remote_login_lock:
            _remote_login_pending[device_code] = {
                "device_code": device_code,
                "user_code": user_code,
                "login": login,
                "device_name": (req.device_name or "").strip(),
                "requester_ip": requester_ip,
                "status": "pending",
                "created_at": now,
                "created_at_iso": created_at_iso,
                "expires_at": now + _REMOTE_LOGIN_DEVICE_TTL_SECONDS,
            }
        if has_feishu_approval:
            with _remote_login_lock:
                record = dict(_remote_login_pending.get(device_code) or {})
            sent_ok, err = await _send_remote_login_approval_card(record)
            if not sent_ok:
                with _remote_login_lock:
                    _remote_login_pending.pop(device_code, None)
                return {
                    "ok": False,
                    "error": "approval_notify_failed",
                    "message": f"Failed to send Feishu approval card: {err}",
                }
            return {
                "ok": True,
                "device_code": device_code,
                "status": "authorization_pending",
                "mode": "feishu_card",
                "expires_in": _REMOTE_LOGIN_DEVICE_TTL_SECONDS,
                "interval_seconds": _REMOTE_LOGIN_POLL_INTERVAL_SECONDS,
            }
        return {
            "ok": True,
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": "/remote/login",
            "expires_in": _REMOTE_LOGIN_DEVICE_TTL_SECONDS,
            "interval_seconds": _REMOTE_LOGIN_POLL_INTERVAL_SECONDS,
        }

    @app.post("/remote/login/poll")
    async def remote_login_poll(req: RemoteLoginPollRequest) -> dict[str, object]:
        now = time.time()
        _remote_login_cleanup_expired(now)
        code = (req.device_code or "").strip()
        if not code:
            return {"ok": False, "status": "invalid_request"}
        with _remote_login_lock:
            record = _remote_login_pending.get(code)
            if record is None:
                return {"ok": False, "status": "expired_or_invalid"}
            status = str(record.get("status") or "pending")
            if status == "pending":
                return {
                    "ok": False,
                    "status": "authorization_pending",
                    "interval_seconds": _REMOTE_LOGIN_POLL_INTERVAL_SECONDS,
                }
            if status == "denied":
                _remote_login_pending.pop(code, None)
                return {"ok": False, "status": "access_denied"}
            if status == "approved":
                token_value = str(record.get("token") or "").strip()
                login = str(record.get("login") or "").strip()
                _remote_login_pending.pop(code, None)
                return {
                    "ok": True,
                    "status": "approved",
                    "login": login,
                    "token": token_value,
                }
            return {"ok": False, "status": "invalid_state"}

    @app.post("/remote/login/approve")
    async def remote_login_approve(req: RemoteLoginApproveRequest) -> dict[str, object]:
        expected_secret = remote_login_approver_secret()
        supplied_secret = (req.approver_secret or "").strip()
        if not expected_secret:
            return {"ok": False, "error": "login_panel_disabled"}
        if not secrets.compare_digest(supplied_secret, expected_secret):
            return {"ok": False, "error": "forbidden"}

        now = time.time()
        _remote_login_cleanup_expired(now)
        user_code = (req.user_code or "").strip().upper()
        if not user_code:
            return {"ok": False, "error": "invalid_user_code"}
        with _remote_login_lock:
            target_device_code = ""
            target_record: dict[str, object] | None = None
            for device_code, record in _remote_login_pending.items():
                if str(record.get("user_code") or "").upper() == user_code:
                    target_device_code = device_code
                    target_record = record
                    break
            if target_record is None:
                return {"ok": False, "error": "user_code_not_found"}
            if not bool(req.approve):
                target_record["status"] = "denied"
                return {"ok": True, "status": "denied"}
            login = str(target_record.get("login") or "").strip()
            token_value = secrets.token_urlsafe(32)
            register_remote_worker_token(login, token_value)
            target_record["status"] = "approved"
            target_record["approved_at"] = now
            target_record["token"] = token_value
            _remote_login_pending[target_device_code] = target_record
            return {"ok": True, "status": "approved", "login": login}

    @app.websocket("/remote/worker/{login}")
    async def worker_endpoint(websocket: WebSocket, login: str) -> None:
        supplied = str(websocket.query_params.get("token") or "").strip()
        allowed, reason = verify_remote_worker_token(
            login=login,
            supplied_token=supplied,
            token_override=token,
        )
        if not allowed:
            logger.warning(
                "remote worker rejected: login=%s reason=%s "
                "(check token registry, MACCHIATO_REMOTE_TOKENS, or "
                "MACCHIATO_REMOTE_TOKEN vs macchiato-remote login --token)",
                (login or "").strip(),
                reason,
            )
            await websocket.close(code=1008)
            return

        await websocket.accept()
        login_s = (login or "").strip()
        conn = RemoteWorkerConnection(login=login_s, send_json=websocket.send_json)
        registry = get_remote_worker_registry()
        await registry.register(conn)
        logger.info("remote worker connected: login=%s", login_s)
        try:
            while True:
                message = await websocket.receive_json()
                if isinstance(message, dict):
                    if message.get("type") == "worker_hello":
                        conn.hello_meta = {
                            "protocol_version": message.get("protocol_version"),
                            "package_version": message.get("package_version"),
                            "capabilities": list(message.get("capabilities") or []),
                        }
                        logger.info(
                            "remote worker hello: login=%s protocol=%s package=%s caps=%s",
                            login_s,
                            message.get("protocol_version"),
                            message.get("package_version"),
                            message.get("capabilities"),
                        )
                        continue
                    conn.handle_message(message)
        except WebSocketDisconnect:
            logger.info("remote worker disconnected: login=%s", login_s)
        finally:
            await registry.unregister(login_s, conn)

    return app


def _login_panel_html() -> str:
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>macchiato remote login</title>
    <style>
      body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        margin: 0;
        background: #0b1020;
        color: #e5e7eb;
      }
      .wrap {
        max-width: 760px;
        margin: 56px auto;
        padding: 0 20px;
      }
      .card {
        background: #111827;
        border: 1px solid #374151;
        border-radius: 12px;
        padding: 24px;
      }
      h1 {
        margin-top: 0;
        font-size: 24px;
      }
      code {
        background: #1f2937;
        border-radius: 6px;
        padding: 2px 6px;
      }
      .muted {
        color: #9ca3af;
      }
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="card">
        <h1>macchiato remote login panel</h1>
        <p>Approve a pending remote login request with server approver secret.</p>
        <div>
          <label>User code<br /><input id="user_code" placeholder="ABCD-EFGH" /></label>
        </div>
        <div style="margin-top: 10px;">
          <label>Approver secret<br /><input id="approver_secret" type="password" /></label>
        </div>
        <div style="margin-top: 14px;">
          <button onclick="approve(true)">Approve</button>
          <button onclick="approve(false)">Deny</button>
        </div>
        <p id="result" class="muted"></p>
        <p class="muted">
          Current worker websocket endpoint: <code>/remote/worker/{login}</code><br />
          Health endpoint: <code>/remote/healthz</code>
        </p>
        <p class="muted">
          Device-login flow endpoint: <code>/remote/login/start</code> + <code>/remote/login/poll</code><br />
          If <code>MACCHIATO_REMOTE_LOGIN_BOOTSTRAP_TOKEN</code> is configured, CLI can exchange directly without panel approval.
        </p>
      </div>
    </div>
    <script>
      async function approve(allow) {
        const userCode = document.getElementById("user_code").value.trim();
        const approverSecret = document.getElementById("approver_secret").value;
        const result = document.getElementById("result");
        if (!userCode || !approverSecret) {
          result.textContent = "Please provide user code and approver secret.";
          return;
        }
        result.textContent = "Submitting...";
        try {
          const resp = await fetch("/remote/login/approve", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              user_code: userCode,
              approver_secret: approverSecret,
              approve: allow
            })
          });
          const data = await resp.json();
          if (!data.ok) {
            result.textContent = "Failed: " + (data.error || "unknown_error");
            return;
          }
          result.textContent = "Success: " + (data.status || "ok");
        } catch (err) {
          result.textContent = "Request failed: " + String(err);
        }
      }
    </script>
  </body>
</html>
"""


async def run_remote_worker_server_until_stopped(
    stop_event: asyncio.Event,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    token: Optional[str] = None,
) -> None:
    bind_host = host or remote_server_host()
    bind_port = int(port or remote_server_port())
    app = create_remote_worker_app(token=token)
    config = uvicorn.Config(
        app,
        host=bind_host,
        port=bind_port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="remote-worker-websocket-server")
    logger.info(
        "remote worker server starting on ws://%s:%s "
        "(login panel: http://%s:%s/remote/login)",
        bind_host,
        bind_port,
        bind_host,
        bind_port,
    )
    try:
        await stop_event.wait()
    finally:
        server.should_exit = True
        await asyncio.gather(task, return_exceptions=True)
