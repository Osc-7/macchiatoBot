"""WebSocket server for local macchiato-remote workers."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from agent_core.remote.worker_registry import (
    RemoteWorkerConnection,
    get_remote_worker_registry,
)

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


def create_remote_worker_app(*, token: Optional[str] = None) -> FastAPI:
    if not (token or remote_server_token()):
        logger.warning(
            "MACCHIATO_REMOTE_TOKEN is unset: remote worker WebSocket has no shared-secret auth"
        )
    app = FastAPI(title="macchiato remote worker gateway")

    @app.websocket("/remote/worker/{login}")
    async def worker_endpoint(websocket: WebSocket, login: str) -> None:
        expected = token or remote_server_token()
        supplied = str(websocket.query_params.get("token") or "").strip()
        if expected and supplied != expected:
            logger.warning(
                "remote worker rejected: login=%s reason=token_mismatch "
                "(check MACCHIATO_REMOTE_TOKEN vs macchiato-remote login --token)",
                (login or "").strip(),
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
