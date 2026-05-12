"""WebSocket server for local macchiato-remote workers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from agent_core.remote.worker_registry import (RemoteWorkerConnection,
                                               get_remote_worker_registry)
from macchiato_remote.tokens import (expected_token_matches,
                                     load_registered_remote_worker_tokens,
                                     remote_token_registry_path)

logger = logging.getLogger(__name__)

# 默认 WebSocket 端口（避免与常见 8765 开发端口冲突）；可用 MACCHIATO_REMOTE_PORT 覆盖。
DEFAULT_REMOTE_WORKER_WEBSOCKET_PORT = 9380


def remote_server_enabled() -> bool:
    raw = os.environ.get("MACCHIATO_REMOTE_SERVER_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


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
                    conn.handle_message(message)
        except WebSocketDisconnect:
            logger.info("remote worker disconnected: login=%s", login_s)
        finally:
            await registry.unregister(login_s, conn)

    return app


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
    logger.info("remote worker server starting on ws://%s:%s", bind_host, bind_port)
    try:
        await stop_event.wait()
    finally:
        server.should_exit = True
        await asyncio.gather(task, return_exceptions=True)
