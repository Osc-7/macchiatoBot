"""Remote worker client that connects a local machine to the cloud daemon."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode, urlparse, urlunparse

from macchiato_remote.protocol import (
    REMOTE_PROTOCOL_VERSION,
    REMOTE_WORKER_CAPABILITIES,
    REMOTE_WORKSPACE_MOUNT,
    RemoteCommandRequest,
    RemoteCommandResult,
    RemoteFileBlobReadRequest,
    RemoteFileBlobReadResult,
    RemoteFileReadRequest,
    RemoteFileReadResult,
    RemoteFileWriteRequest,
    RemoteFileWriteResult,
    RemoteJobStartRequest,
    RemoteJobStartResult,
    RemoteJobStatusRequest,
    RemoteJobStatusResult,
    RemoteJobStopRequest,
    RemoteJobStopResult,
    RemoteJobTailRequest,
    RemoteJobTailResult,
    RemoteShellCaptureRequest,
    RemoteShellCaptureResult,
    RemoteShellResetRequest,
    RemoteShellResetResult,
    RemoteWorkspaceCloseRequest,
    RemoteWorkspaceCloseResult,
    RemoteWorkspaceOpenRequest,
    RemoteWorkspaceOpenResult,
)
from macchiato_remote.runtime.files import (
    read_workspace_blob,
    read_workspace_text,
    write_workspace_text,
)
from macchiato_remote.runtime.jobs import RemoteJobRegistry
from macchiato_remote.runtime.shell import LocalShellConfig, LocalShellSession


def package_version() -> str:
    """Installed distribution version (macchiato-remote or macchiato-bot)."""
    from importlib.metadata import PackageNotFoundError, version

    for dist in ("macchiato-remote", "macchiato-bot"):
        try:
            return version(dist)
        except PackageNotFoundError:
            continue
    return "0.0.0+dev"


def worker_hello_payload() -> Dict[str, Any]:
    return {
        "type": "worker_hello",
        "protocol_version": REMOTE_PROTOCOL_VERSION,
        "capabilities": list(REMOTE_WORKER_CAPABILITIES),
        "package_version": package_version(),
    }


def normalize_remote_server_url(server: str) -> str:
    """Normalize server URL for remote worker connection.

    Accepts shorthand like ``149.28.149.135:9380`` and rewrites it to
    ``http://149.28.149.135:9380`` so websocket URL construction is stable.
    """
    s = (server or "").strip().rstrip("/")
    if not s:
        return ""
    # `host:port` without scheme is common for CLI usage.
    if "://" not in s:
        return f"http://{s}"
    parsed = urlparse(s)
    # Defensive fallback for malformed schemes like `http:1.2.3.4:9380`.
    if not parsed.netloc and parsed.path and ":" in parsed.path:
        return f"http://{parsed.path}"
    return s


def raw_websocket_handshake_probe(
    *,
    server: str,
    login: str,
    token: Optional[str] = None,
    timeout_seconds: float = 20.0,
) -> str:
    """Send a minimal WebSocket upgrade over a blocking :class:`socket.socket`.

    Uses only the stdlib socket stack (no ``asyncio``, no ``HTTP_PROXY``), so
    Clash / system proxies that only affect high-level HTTP stacks can be ruled
    out when comparing with :func:`websockets.connect` behaviour.
    """
    import base64
    import socket as socket_mod

    c = RemoteWorkerClient(server=server, login=login, token=token)
    url = c._websocket_url()
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise ValueError("invalid server URL (no host)")
    port = int(parsed.port or (443 if parsed.scheme == "wss" else 80))
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    infos = socket_mod.getaddrinfo(
        host, port, type=socket_mod.SOCK_STREAM, proto=socket_mod.IPPROTO_TCP
    )
    if not infos:
        raise OSError(f"no address for {host!r}:{port}")
    fam, _, _, _, sockaddr = infos[0]
    sock = socket_mod.socket(fam, socket_mod.SOCK_STREAM)
    connect_timeout = min(12.0, max(3.0, timeout_seconds * 0.6))
    read_timeout = max(8.0, timeout_seconds)
    try:
        sock.settimeout(connect_timeout)
        try:
            sock.connect(sockaddr)
        except TimeoutError as exc:
            raise TimeoutError(
                f"TCP connect to {host}:{port} timed out after {connect_timeout:.0f}s. "
                "On this Mac run: "
                f"`nc -zv {host} {port}` — if it also hangs, check: (1) cloud security group "
                f"allows inbound TCP {port}; (2) IP is correct; (3) after quitting VPN/TUN, "
                "reboot Mac or fix routes (stale utun routes can black-hole traffic)."
            ) from exc
        if parsed.scheme == "wss":
            import ssl

            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: macchiato-remote-probe/1\r\n"
            f"\r\n"
        ).encode("ascii", errors="surrogateescape")
        sock.sendall(req)
        sock.settimeout(read_timeout)
        parts: list[bytes] = []
        total = 0
        while total < 65536:
            try:
                chunk = sock.recv(8192)
            except TimeoutError as exc:
                raise TimeoutError(
                    f"No HTTP bytes from {host}:{port} within {read_timeout:.0f}s after "
                    "sending WebSocket upgrade. Is automation_daemon listening? "
                    "`ss -tlnp | grep` on the server. If TCP worked before and only "
                    "fails after VPN, reboot or flush routes."
                ) from exc
            if not chunk:
                break
            parts.append(chunk)
            total += len(chunk)
            if len(chunk) < 8192:
                break
        raw = b"".join(parts)
        return raw.decode("utf-8", errors="replace")
    finally:
        try:
            sock.close()
        except OSError:
            pass


class RemoteWorkerClient:
    def __init__(
        self,
        *,
        server: str,
        login: str,
        token: Optional[str] = None,
        shell_path: str = "/bin/bash",
    ) -> None:
        self.server = normalize_remote_server_url(server)
        self.login = login.strip()
        self.token = (token or "").strip() or None
        self.shell_path = shell_path
        self._sessions: Dict[str, LocalShellSession] = {}
        self._jobs = RemoteJobRegistry()

    async def run_forever(self) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "macchiato-remote start requires websockets: "
                "uv tool install macchiato-remote  (or: pip install macchiato-remote)"
            ) from exc

        url = self._websocket_url()
        public = self._websocket_url_for_log()
        _hinted_invalid_http = False
        while True:
            try:
                print(
                    f"macchiato-remote: connecting to {public} …",
                    file=sys.stderr,
                    flush=True,
                )
                # websockets 默认 proxy=True 会走 HTTP_PROXY/HTTPS_PROXY；多数代理对直连 IP
                # 的 WS 升级返回非 HTTP 响应 → InvalidMessage('did not receive a valid HTTP response')。
                # 连自己的云主机应直连；若必须走代理：export MACCHIATO_REMOTE_USE_SYSTEM_PROXY=1
                use_proxy: bool | str | None = (
                    True
                    if os.environ.get("MACCHIATO_REMOTE_USE_SYSTEM_PROXY", "")
                    .strip()
                    .lower()
                    in {"1", "true", "yes"}
                    else None
                )
                async with websockets.connect(
                    url,
                    proxy=use_proxy,
                    open_timeout=30.0,
                    ping_interval=60.0,
                    ping_timeout=120.0,
                ) as ws:
                    await ws.send(
                        json.dumps(worker_hello_payload(), ensure_ascii=False)
                    )
                    print(
                        "macchiato-remote: connected (login registered on server). "
                        f"protocol={REMOTE_PROTOCOL_VERSION} package={package_version()}. "
                        "Leave this process running; use /remote-use in Feishu.",
                        file=sys.stderr,
                        flush=True,
                    )
                    async for raw in ws:
                        message = json.loads(raw)
                        response = await self._handle_message(message)
                        if response is not None:
                            await ws.send(json.dumps(response, ensure_ascii=False))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    f"macchiato-remote: connection lost or failed: {exc!r}. Retrying in 3s…",
                    file=sys.stderr,
                    flush=True,
                )
                cause = getattr(exc, "__cause__", None)
                if cause is not None:
                    print(
                        f"macchiato-remote: underlying cause ({type(cause).__name__}): {cause!r}",
                        file=sys.stderr,
                        flush=True,
                    )
                if (
                    not _hinted_invalid_http
                    and "valid HTTP" in str(exc)
                    and not os.environ.get(
                        "MACCHIATO_REMOTE_USE_SYSTEM_PROXY", ""
                    ).strip()
                ):
                    _hinted_invalid_http = True
                    print(
                        "macchiato-remote: hint — try `macchiato-remote probe` (raw socket, "
                        "ignores HTTP_PROXY). If probe shows HTTP/1.1 101 but start still fails, "
                        "file an issue with the probe vs start output. If Clash uses TUN mode, "
                        "temporarily disable TUN or bypass this host; env http_proxy=empty is not enough.",
                        file=sys.stderr,
                        flush=True,
                    )
                await asyncio.sleep(3)

    async def close(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()

    def _websocket_url(self) -> str:
        parsed = urlparse(self.server)
        scheme = parsed.scheme
        if scheme == "https":
            scheme = "wss"
        elif scheme == "http":
            scheme = "ws"
        elif scheme not in {"ws", "wss"}:
            scheme = "wss"
        netloc = parsed.netloc
        if not netloc and parsed.path and ":" in parsed.path:
            # Backward-compatibility for legacy configs with missing scheme.
            netloc = parsed.path
            base_path = ""
        else:
            base_path = parsed.path
        path = base_path.rstrip("/") + f"/remote/worker/{quote(self.login)}"
        query = parsed.query
        if self.token:
            extra = urlencode({"token": self.token})
            query = f"{query}&{extra}" if query else extra
        return urlunparse((scheme, netloc, path, "", query, ""))

    def _websocket_url_for_log(self) -> str:
        """Same URL as connect but without query string (hides token)."""
        parsed = urlparse(self._websocket_url())
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    async def _handle_message(
        self, message: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        msg_type = str(message.get("type") or "")
        payload = message.get("request")
        if not isinstance(payload, dict):
            return None
        if msg_type == "open_workspace":
            req = RemoteWorkspaceOpenRequest.model_validate(payload)
            result = await self._open_workspace(req)
            return {"type": "open_workspace_result", "result": result.model_dump()}
        if msg_type == "close_workspace":
            req = RemoteWorkspaceCloseRequest.model_validate(payload)
            result = await self._close_workspace(req)
            return {"type": "close_workspace_result", "result": result.model_dump()}
        if msg_type == "exec":
            req = RemoteCommandRequest.model_validate(payload)
            result = await self._execute(req)
            return {"type": "exec_result", "result": result.model_dump()}
        if msg_type == "file_read":
            req = RemoteFileReadRequest.model_validate(payload)
            result = await self._file_read(req)
            return {"type": "file_read_result", "result": result.model_dump()}
        if msg_type == "file_write":
            req = RemoteFileWriteRequest.model_validate(payload)
            result = await self._file_write(req)
            return {"type": "file_write_result", "result": result.model_dump()}
        if msg_type == "file_blob_read":
            req = RemoteFileBlobReadRequest.model_validate(payload)
            result = await self._file_blob_read(req)
            return {"type": "file_blob_read_result", "result": result.model_dump()}
        if msg_type == "reset_shell":
            req = RemoteShellResetRequest.model_validate(payload)
            result = await self._reset_shell(req)
            return {"type": "reset_shell_result", "result": result.model_dump()}
        if msg_type == "shell_capture":
            req = RemoteShellCaptureRequest.model_validate(payload)
            result = await self._shell_capture(req)
            return {"type": "shell_capture_result", "result": result.model_dump()}
        if msg_type == "job_start":
            req = RemoteJobStartRequest.model_validate(payload)
            result = await self._job_start(req)
            return {"type": "job_start_result", "result": result.model_dump()}
        if msg_type == "job_status":
            req = RemoteJobStatusRequest.model_validate(payload)
            result = await self._job_status(req)
            return {"type": "job_status_result", "result": result.model_dump()}
        if msg_type == "job_tail":
            req = RemoteJobTailRequest.model_validate(payload)
            result = await self._job_tail(req)
            return {"type": "job_tail_result", "result": result.model_dump()}
        if msg_type == "job_stop":
            req = RemoteJobStopRequest.model_validate(payload)
            result = await self._job_stop(req)
            return {"type": "job_stop_result", "result": result.model_dump()}
        return None

    async def _open_workspace(
        self, req: RemoteWorkspaceOpenRequest
    ) -> RemoteWorkspaceOpenResult:
        try:
            root = Path(req.requested_path).expanduser().resolve()
            if not root.is_dir():
                return RemoteWorkspaceOpenResult(
                    request_id=req.request_id,
                    session_id=req.session_id,
                    success=False,
                    message=f"路径不存在或不是目录: {root}",
                    error="PATH_NOT_DIRECTORY",
                )
            old = self._sessions.pop(req.session_id, None)
            if old is not None:
                await old.close()
            session = LocalShellSession(
                LocalShellConfig(root=root, shell_path=self.shell_path)
            )
            await session.start()
            self._sessions[req.session_id] = session
            self._jobs.open_session(req.session_id, root)
            return RemoteWorkspaceOpenResult(
                request_id=req.request_id,
                session_id=req.session_id,
                success=True,
                resolved_path=str(root),
                device_label=platform.node() or self.login,
                message="远程工作区已打开",
            )
        except Exception as exc:
            return RemoteWorkspaceOpenResult(
                request_id=req.request_id,
                session_id=req.session_id,
                success=False,
                message=str(exc),
                error="OPEN_WORKSPACE_ERROR",
            )

    async def _close_workspace(
        self, req: RemoteWorkspaceCloseRequest
    ) -> RemoteWorkspaceCloseResult:
        session = self._sessions.pop(req.session_id, None)
        if session is not None:
            await session.close()
        self._jobs.close_session(req.session_id)
        return RemoteWorkspaceCloseResult(
            request_id=req.request_id,
            session_id=req.session_id,
            success=True,
            message="远程工作区已关闭",
        )

    async def _execute(self, req: RemoteCommandRequest):
        session = self._sessions.get(req.session_id)
        if session is None:
            return RemoteCommandResult(
                request_id=req.request_id,
                command=req.command,
                stderr=f"remote session is not open: {req.session_id}",
                exit_code=127,
                cwd=req.cwd,
                error="SESSION_NOT_OPEN",
            )
        hard_timeout = req.timeout_seconds
        wait_window_ms = req.wait_window_ms if req.wait_window_ms is not None else 30_000
        if self._is_stateful_shell_command(req.command):
            return await session.execute(
                request_id=req.request_id,
                command=req.command,
                timeout_seconds=hard_timeout,
                output_limit=req.output_limit,
                extra_read_roots=list(req.extra_read_roots or []),
            )
        mgr = self._job_manager_for(req.session_id)
        if mgr is None:
            return RemoteCommandResult(
                request_id=req.request_id,
                command=req.command,
                stderr=f"remote session is not open: {req.session_id}",
                exit_code=127,
                cwd=req.cwd,
                error="SESSION_NOT_OPEN",
            )
        cap = await session.capture_session()
        job_cwd = str(session.root)
        job_env: Dict[str, str] = {}
        if cap is not None:
            job_cwd, job_env = cap
        handle = await mgr.start_job(
            req.command,
            cwd=job_cwd,
            env=job_env,
            timeout_seconds=hard_timeout,
        )
        waited = max(0.0, float(wait_window_ms) / 1000.0)
        end_at = asyncio.get_running_loop().time() + waited
        status = handle
        while status.status == "running" and asyncio.get_running_loop().time() < end_at:
            await asyncio.sleep(0.05)
            latest = await mgr.job_status(handle.job_id)
            if latest is not None:
                status = latest
        if status.status == "running":
            return RemoteCommandResult(
                request_id=req.request_id,
                command=req.command,
                cwd=job_cwd,
                backgrounded=True,
                job_id=handle.job_id,
                job_status=status.status,
                job_log_path=str(handle.log_path),
                job_pid=handle.pid,
            )
        try:
            stdout = status.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stdout = ""
        return RemoteCommandResult(
            request_id=req.request_id,
            command=req.command,
            stdout=stdout,
            stderr="",
            exit_code=int(status.exit_code or 0),
            timed_out=bool(status.timed_out or status.status == "timed_out"),
            truncated=False,
            cwd=job_cwd,
        )

    def _job_manager_for(self, session_id: str):
        return self._jobs.get(session_id)

    @staticmethod
    def _is_stateful_shell_command(command: str) -> bool:
        raw = str(command or "").strip()
        if not raw:
            return False
        lowered = raw.lower()
        if lowered.startswith(
            ("cd ", "export ", "unset ", "alias ", "unalias ", "source ", ". ")
        ):
            return True
        return "() {" in raw

    async def _file_read(self, req: RemoteFileReadRequest) -> RemoteFileReadResult:
        session = self._sessions.get(req.session_id)
        if session is None:
            return RemoteFileReadResult(
                request_id=req.request_id,
                path=req.path,
                content="",
                error="remote session is not open",
            )
        text, truncated, err = read_workspace_text(
            session.root,
            req.path,
            encoding=req.encoding,
            start_line=req.start_line,
            end_line=req.end_line,
        )
        if err:
            return RemoteFileReadResult(
                request_id=req.request_id,
                path=req.path,
                content="",
                truncated=False,
                error=err,
            )
        return RemoteFileReadResult(
            request_id=req.request_id,
            path=req.path,
            content=text,
            encoding=req.encoding,
            truncated=truncated,
        )

    async def _file_write(self, req: RemoteFileWriteRequest) -> RemoteFileWriteResult:
        session = self._sessions.get(req.session_id)
        if session is None:
            return RemoteFileWriteResult(
                request_id=req.request_id,
                path=req.path,
                error="remote session is not open",
            )
        written, err = write_workspace_text(
            session.root,
            req.path,
            req.content,
            encoding=req.encoding,
            mode=req.mode,
        )
        if err:
            return RemoteFileWriteResult(
                request_id=req.request_id,
                path=req.path,
                error=err,
            )
        return RemoteFileWriteResult(
            request_id=req.request_id,
            path=req.path,
            bytes_written=written,
            encoding=req.encoding,
        )

    async def _file_blob_read(
        self, req: RemoteFileBlobReadRequest
    ) -> RemoteFileBlobReadResult:
        session = self._sessions.get(req.session_id)
        if session is None:
            return RemoteFileBlobReadResult(
                request_id=req.request_id,
                path=req.path,
                error="remote session is not open",
            )
        content_b64, name, mime, read_n, truncated, err = read_workspace_blob(
            session.root,
            req.path,
            max_bytes=req.max_bytes,
        )
        if err:
            return RemoteFileBlobReadResult(
                request_id=req.request_id,
                path=req.path,
                error=err,
            )
        return RemoteFileBlobReadResult(
            request_id=req.request_id,
            path=req.path,
            content_base64=content_b64,
            file_name=name,
            mime_type=mime,
            bytes_read=read_n,
            truncated=truncated,
        )

    async def _reset_shell(
        self, req: RemoteShellResetRequest
    ) -> RemoteShellResetResult:
        session = self._sessions.get(req.session_id)
        if session is None:
            return RemoteShellResetResult(
                request_id=req.request_id,
                session_id=req.session_id,
                success=False,
                error="remote session is not open",
            )
        try:
            await session.close()
            await session.start()
            return RemoteShellResetResult(
                request_id=req.request_id,
                session_id=req.session_id,
                success=True,
                message="远程 bash 已重置",
            )
        except Exception as exc:
            return RemoteShellResetResult(
                request_id=req.request_id,
                session_id=req.session_id,
                success=False,
                error=str(exc),
            )

    async def _shell_capture(
        self, req: RemoteShellCaptureRequest
    ) -> RemoteShellCaptureResult:
        session = self._sessions.get(req.session_id)
        if session is None:
            return RemoteShellCaptureResult(
                request_id=req.request_id,
                session_id=req.session_id,
                error="SESSION_NOT_OPEN",
            )
        cap = await session.capture_session()
        if cap is None:
            return RemoteShellCaptureResult(
                request_id=req.request_id,
                session_id=req.session_id,
                cwd=str(session.root),
                error="CAPTURE_FAILED",
            )
        cwd, env = cap
        return RemoteShellCaptureResult(
            request_id=req.request_id,
            session_id=req.session_id,
            cwd=cwd,
            env=env,
        )

    async def _job_start(self, req: RemoteJobStartRequest) -> RemoteJobStartResult:
        mgr = self._job_manager_for(req.session_id)
        if mgr is None:
            return RemoteJobStartResult(
                request_id=req.request_id,
                session_id=req.session_id,
                job_id="",
                error="SESSION_NOT_OPEN",
            )
        cwd = req.cwd
        if cwd == REMOTE_WORKSPACE_MOUNT:
            cwd = str(mgr._workspace_root)
        try:
            handle = await mgr.start_job(
                req.command,
                cwd=cwd,
                env=dict(req.env) if req.env else None,
                timeout_seconds=req.timeout_seconds,
            )
        except Exception as exc:
            return RemoteJobStartResult(
                request_id=req.request_id,
                session_id=req.session_id,
                job_id="",
                error=str(exc),
            )
        return RemoteJobStartResult(
            request_id=req.request_id,
            session_id=req.session_id,
            job_id=handle.job_id,
            pid=handle.pid,
            log_path=str(handle.log_path),
            status=handle.status,
        )

    async def _job_status(self, req: RemoteJobStatusRequest) -> RemoteJobStatusResult:
        mgr = self._job_manager_for(req.session_id)
        if mgr is None:
            return RemoteJobStatusResult(
                request_id=req.request_id,
                session_id=req.session_id,
                job_id=req.job_id,
                status="unknown",
                error="SESSION_NOT_OPEN",
            )
        handle = await mgr.job_status(req.job_id)
        if handle is None:
            return RemoteJobStatusResult(
                request_id=req.request_id,
                session_id=req.session_id,
                job_id=req.job_id,
                status="unknown",
                error="JOB_NOT_FOUND",
            )
        return RemoteJobStatusResult(
            request_id=req.request_id,
            session_id=req.session_id,
            job_id=handle.job_id,
            status=handle.status,
            command=handle.command,
            pid=handle.pid,
            exit_code=handle.exit_code,
            timed_out=handle.timed_out,
            duration_seconds=round(handle.duration_seconds, 2),
            log_path=str(handle.log_path),
        )

    async def _job_tail(self, req: RemoteJobTailRequest) -> RemoteJobTailResult:
        mgr = self._job_manager_for(req.session_id)
        if mgr is None:
            return RemoteJobTailResult(
                request_id=req.request_id,
                session_id=req.session_id,
                job_id=req.job_id,
                status="unknown",
                error="SESSION_NOT_OPEN",
            )
        data = await mgr.job_tail(
            req.job_id, lines=max(1, int(req.lines)), offset=max(0, int(req.offset))
        )
        if data is None:
            return RemoteJobTailResult(
                request_id=req.request_id,
                session_id=req.session_id,
                job_id=req.job_id,
                status="unknown",
                error="JOB_NOT_FOUND",
            )
        return RemoteJobTailResult(
            request_id=req.request_id,
            session_id=req.session_id,
            job_id=req.job_id,
            status=str(data.get("status") or ""),
            total_lines=int(data.get("total_lines") or 0),
            read_lines=len(data.get("head_lines") or [])
            + len(data.get("tail_lines") or []),
            offset=int(data.get("offset") or 0),
            log_path=str(data.get("log_path") or ""),
            head_lines=list(data.get("head_lines") or []),
            tail_lines=list(data.get("tail_lines") or []),
        )

    async def _job_stop(self, req: RemoteJobStopRequest) -> RemoteJobStopResult:
        mgr = self._job_manager_for(req.session_id)
        if mgr is None:
            return RemoteJobStopResult(
                request_id=req.request_id,
                session_id=req.session_id,
                job_id=req.job_id,
                success=False,
                error="SESSION_NOT_OPEN",
            )
        ok = await mgr.stop_job(req.job_id, signal_name=req.signal)
        return RemoteJobStopResult(
            request_id=req.request_id,
            session_id=req.session_id,
            job_id=req.job_id,
            success=ok,
            error=None if ok else "STOP_FAILED",
        )
