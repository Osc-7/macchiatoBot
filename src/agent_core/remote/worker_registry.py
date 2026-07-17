"""In-process registry for connected remote workers."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from macchiato_remote.protocol import (
    REMOTE_BLOB_MAX_BYTES,
    REMOTE_WORKSPACE_MOUNT,
    RemoteCommandRequest,
    RemoteCommandResult,
    RemoteFileBlobReadRequest,
    RemoteFileBlobReadResult,
    RemoteFileBlobWriteRequest,
    RemoteFileBlobWriteResult,
    RemoteFileReadRequest,
    RemoteFileReadResult,
    RemoteFileWriteRequest,
    RemoteFileWriteResult,
    RemotePermissionProfile,
    RemoteShellResetRequest,
    RemoteShellResetResult,
    RemoteWorkspaceCloseRequest,
    RemoteWorkspaceCloseResult,
    RemoteWorkspaceOpenRequest,
    RemoteWorkspaceOpenResult,
)

SendJson = Callable[[Dict[str, Any]], Awaitable[None]]


@dataclass
class RemoteWorkerConnection:
    """A live worker connection owned by the daemon process."""

    login: str
    send_json: SendJson
    pending: Dict[str, asyncio.Future[Dict[str, Any]]] = field(default_factory=dict)
    hello_meta: Dict[str, Any] = field(default_factory=dict)

    async def request(
        self,
        message_type: str,
        payload: Dict[str, Any],
        *,
        timeout_seconds: float = 300.0,
    ) -> Dict[str, Any]:
        request_id = str(payload.get("request_id") or uuid.uuid4().hex)
        payload = {**payload, "request_id": request_id}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self.pending[request_id] = fut
        try:
            await self.send_json({"type": message_type, "request": payload})
            return await asyncio.wait_for(fut, timeout=timeout_seconds)
        finally:
            self.pending.pop(request_id, None)

    def handle_message(self, message: Dict[str, Any]) -> None:
        payload = message.get("result")
        if not isinstance(payload, dict):
            payload = message
        request_id = str(
            message.get("request_id") or payload.get("request_id") or ""
        ).strip()
        if not request_id:
            return
        fut = self.pending.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(payload)

    def fail_pending(self, exc: BaseException) -> None:
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self.pending.clear()


class RemoteWorkerRegistry:
    """Process-local registry keyed by user-chosen remote login alias."""

    def __init__(self) -> None:
        self._connections: Dict[str, RemoteWorkerConnection] = {}
        self._lock = asyncio.Lock()

    async def register(self, connection: RemoteWorkerConnection) -> None:
        login = connection.login.strip()
        if not login:
            raise ValueError("login must not be blank")
        async with self._lock:
            old = self._connections.get(login)
            if old is not None and old is not connection:
                old.fail_pending(RuntimeError("remote worker was replaced"))
            self._connections[login] = connection

    async def unregister(
        self, login: str, connection: Optional[RemoteWorkerConnection] = None
    ) -> None:
        key = (login or "").strip()
        async with self._lock:
            current = self._connections.get(key)
            if current is None:
                return
            if connection is not None and current is not connection:
                return
            current.fail_pending(RuntimeError("remote worker disconnected"))
            self._connections.pop(key, None)

    async def get(self, login: str) -> Optional[RemoteWorkerConnection]:
        key = (login or "").strip()
        async with self._lock:
            return self._connections.get(key)

    async def require(self, login: str) -> RemoteWorkerConnection:
        conn = await self.get(login)
        if conn is None:
            raise RuntimeError(f"远程 worker 未连接: {login}")
        return conn

    async def list_logins(self) -> list[str]:
        async with self._lock:
            return sorted(self._connections)

    async def open_workspace(
        self,
        *,
        login: str,
        session_id: str,
        requested_path: str,
        profile: RemotePermissionProfile = "dev",
        timeout_seconds: float = 30.0,
    ) -> RemoteWorkspaceOpenResult:
        conn = await self.require(login)
        req = RemoteWorkspaceOpenRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            requested_path=requested_path,
            profile=profile,
        )
        payload = await conn.request(
            "open_workspace",
            req.model_dump(),
            timeout_seconds=timeout_seconds,
        )
        result = RemoteWorkspaceOpenResult.model_validate(payload)
        if not result.success:
            raise RuntimeError(result.message or result.error or "远程工作区打开失败")
        return result

    async def close_workspace(
        self,
        *,
        login: str,
        session_id: str,
        timeout_seconds: float = 10.0,
    ) -> RemoteWorkspaceCloseResult:
        conn = await self.require(login)
        req = RemoteWorkspaceCloseRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
        )
        payload = await conn.request(
            "close_workspace",
            req.model_dump(),
            timeout_seconds=timeout_seconds,
        )
        return RemoteWorkspaceCloseResult.model_validate(payload)

    async def execute_command(
        self,
        *,
        login: str,
        session_id: str,
        command: str,
        timeout_seconds: Optional[float] = None,
        wait_window_ms: Optional[int] = None,
        wait_for_completion: bool = False,
        output_limit: Optional[int] = None,
        extra_read_roots: Optional[list[str]] = None,
    ) -> RemoteCommandResult:
        conn = await self.require(login)
        req = RemoteCommandRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            command=command,
            cwd=REMOTE_WORKSPACE_MOUNT,
            timeout_seconds=timeout_seconds,
            wait_window_ms=wait_window_ms,
            wait_for_completion=bool(wait_for_completion),
            output_limit=output_limit,
            extra_read_roots=list(extra_read_roots or []),
        )
        payload = await conn.request(
            "exec",
            req.model_dump(),
            timeout_seconds=float(timeout_seconds or 300.0) + 5.0,
        )
        return RemoteCommandResult.model_validate(payload)

    async def file_read(
        self,
        *,
        login: str,
        session_id: str,
        path: str,
        encoding: str = "utf-8",
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        timeout_seconds: float = 120.0,
    ) -> RemoteFileReadResult:
        conn = await self.require(login)
        req = RemoteFileReadRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            path=path,
            encoding=encoding,
            start_line=start_line,
            end_line=end_line,
        )
        payload = await conn.request(
            "file_read",
            req.model_dump(),
            timeout_seconds=timeout_seconds,
        )
        return RemoteFileReadResult.model_validate(payload)

    async def file_write(
        self,
        *,
        login: str,
        session_id: str,
        path: str,
        content: str,
        encoding: str = "utf-8",
        mode: str = "overwrite",
        timeout_seconds: float = 120.0,
    ) -> RemoteFileWriteResult:
        conn = await self.require(login)
        req = RemoteFileWriteRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            path=path,
            content=content,
            encoding=encoding,
            mode=mode if mode in {"overwrite", "append"} else "overwrite",  # type: ignore[arg-type]
        )
        payload = await conn.request(
            "file_write",
            req.model_dump(),
            timeout_seconds=timeout_seconds,
        )
        return RemoteFileWriteResult.model_validate(payload)

    async def file_blob_read(
        self,
        *,
        login: str,
        session_id: str,
        path: str,
        max_bytes: int = REMOTE_BLOB_MAX_BYTES,
        timeout_seconds: float = 120.0,
    ) -> RemoteFileBlobReadResult:
        conn = await self.require(login)
        req = RemoteFileBlobReadRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            path=path,
            max_bytes=max(1, int(max_bytes)),
        )
        payload = await conn.request(
            "file_blob_read",
            req.model_dump(),
            timeout_seconds=timeout_seconds,
        )
        return RemoteFileBlobReadResult.model_validate(payload)

    def worker_supports_file_blob_write(self, login: str) -> bool:
        """True when the connected worker advertised ``file_blob_write``."""
        key = (login or "").strip()
        conn = self._connections.get(key)
        if conn is None:
            return False
        caps = set(conn.hello_meta.get("capabilities") or [])
        return "file_blob_write" in caps

    async def file_blob_write(
        self,
        *,
        login: str,
        session_id: str,
        path: str,
        content_base64: str,
        mode: str = "overwrite",
        max_bytes: int = REMOTE_BLOB_MAX_BYTES,
        timeout_seconds: float = 120.0,
    ) -> RemoteFileBlobWriteResult:
        conn = await self.require(login)
        if not self.worker_supports_file_blob_write(login):
            return RemoteFileBlobWriteResult(
                request_id=uuid.uuid4().hex,
                path=path,
                error="CAPABILITY_MISSING:file_blob_write",
            )
        req = RemoteFileBlobWriteRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            path=path,
            content_base64=content_base64,
            mode=mode if mode in {"overwrite", "append"} else "overwrite",  # type: ignore[arg-type]
            max_bytes=max(1, int(max_bytes)),
        )
        payload = await conn.request(
            "file_blob_write",
            req.model_dump(),
            timeout_seconds=timeout_seconds,
        )
        return RemoteFileBlobWriteResult.model_validate(payload)

    async def reset_remote_shell(
        self,
        *,
        login: str,
        session_id: str,
        timeout_seconds: float = 30.0,
    ) -> RemoteShellResetResult:
        conn = await self.require(login)
        req = RemoteShellResetRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
        )
        payload = await conn.request(
            "reset_shell",
            req.model_dump(),
            timeout_seconds=timeout_seconds,
        )
        return RemoteShellResetResult.model_validate(payload)

    async def capture_remote_shell(
        self,
        *,
        login: str,
        session_id: str,
        timeout_seconds: float = 15.0,
    ):
        from macchiato_remote.protocol import (
            RemoteShellCaptureRequest,
            RemoteShellCaptureResult,
        )

        conn = await self.require(login)
        req = RemoteShellCaptureRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
        )
        payload = await conn.request(
            "shell_capture",
            req.model_dump(),
            timeout_seconds=timeout_seconds,
        )
        return RemoteShellCaptureResult.model_validate(payload)

    async def start_job(
        self,
        *,
        login: str,
        session_id: str,
        command: str,
        cwd: str = REMOTE_WORKSPACE_MOUNT,
        timeout_seconds: Optional[float] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> "RemoteJobStartResult":
        from macchiato_remote.protocol import (
            RemoteJobStartRequest,
            RemoteJobStartResult,
        )

        conn = await self.require(login)
        req = RemoteJobStartRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            command=command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            env=dict(env or {}),
        )
        payload = await conn.request(
            "job_start",
            req.model_dump(),
            timeout_seconds=10.0,
        )
        return RemoteJobStartResult.model_validate(payload)

    async def job_status(
        self,
        *,
        login: str,
        session_id: str,
        job_id: str,
    ) -> "RemoteJobStatusResult":
        from macchiato_remote.protocol import (
            RemoteJobStatusRequest,
            RemoteJobStatusResult,
        )

        conn = await self.require(login)
        req = RemoteJobStatusRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            job_id=job_id,
        )
        payload = await conn.request(
            "job_status",
            req.model_dump(),
            timeout_seconds=10.0,
        )
        return RemoteJobStatusResult.model_validate(payload)

    async def job_tail(
        self,
        *,
        login: str,
        session_id: str,
        job_id: str,
        lines: int = 200,
        offset: int = 0,
    ) -> "RemoteJobTailResult":
        from macchiato_remote.protocol import (
            RemoteJobTailRequest,
            RemoteJobTailResult,
        )

        conn = await self.require(login)
        req = RemoteJobTailRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            job_id=job_id,
            lines=lines,
            offset=offset,
        )
        payload = await conn.request(
            "job_tail",
            req.model_dump(),
            timeout_seconds=30.0,
        )
        return RemoteJobTailResult.model_validate(payload)

    async def stop_job(
        self,
        *,
        login: str,
        session_id: str,
        job_id: str,
        signal: str = "SIGTERM",
    ) -> "RemoteJobStopResult":
        from macchiato_remote.protocol import (
            RemoteJobStopRequest,
            RemoteJobStopResult,
        )

        conn = await self.require(login)
        req = RemoteJobStopRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            job_id=job_id,
            signal=signal,
        )
        payload = await conn.request(
            "job_stop",
            req.model_dump(),
            timeout_seconds=10.0,
        )
        return RemoteJobStopResult.model_validate(payload)

    async def mcp_ensure(
        self,
        *,
        login: str,
        session_id: str,
        servers: list[str],
        timeout_seconds: float = 60.0,
    ):
        from macchiato_remote.protocol import (
            RemoteMcpEnsureRequest,
            RemoteMcpEnsureResult,
            RemoteMcpServerRef,
        )

        conn = await self.require(login)
        req = RemoteMcpEnsureRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            servers=[RemoteMcpServerRef(name=n) for n in servers],
        )
        payload = await conn.request(
            "mcp_ensure",
            req.model_dump(),
            timeout_seconds=timeout_seconds,
        )
        return RemoteMcpEnsureResult.model_validate(payload)

    async def mcp_list_tools(
        self,
        *,
        login: str,
        session_id: str,
        server_name: str,
        refresh: bool = False,
        timeout_seconds: float = 60.0,
    ):
        from macchiato_remote.protocol import (
            RemoteMcpListToolsRequest,
            RemoteMcpListToolsResult,
        )

        conn = await self.require(login)
        req = RemoteMcpListToolsRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            server_name=server_name,
            refresh=refresh,
        )
        payload = await conn.request(
            "mcp_list_tools",
            req.model_dump(),
            timeout_seconds=timeout_seconds,
        )
        return RemoteMcpListToolsResult.model_validate(payload)

    async def mcp_call_tool(
        self,
        *,
        login: str,
        session_id: str,
        server_name: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ):
        from macchiato_remote.protocol import (
            RemoteMcpCallToolRequest,
            RemoteMcpCallToolResult,
        )

        conn = await self.require(login)
        timeout = float(timeout_seconds) if timeout_seconds is not None else 120.0
        req = RemoteMcpCallToolRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            server_name=server_name,
            tool_name=tool_name,
            arguments=dict(arguments or {}),
            timeout_seconds=timeout_seconds,
        )
        payload = await conn.request(
            "mcp_call_tool",
            req.model_dump(),
            timeout_seconds=timeout + 5.0,
        )
        return RemoteMcpCallToolResult.model_validate(payload)

    async def mcp_shutdown(
        self,
        *,
        login: str,
        session_id: str,
        server_name: Optional[str] = None,
        timeout_seconds: float = 30.0,
    ):
        from macchiato_remote.protocol import (
            RemoteMcpShutdownRequest,
            RemoteMcpShutdownResult,
        )

        conn = await self.require(login)
        req = RemoteMcpShutdownRequest(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            server_name=server_name,
        )
        payload = await conn.request(
            "mcp_shutdown",
            req.model_dump(),
            timeout_seconds=timeout_seconds,
        )
        return RemoteMcpShutdownResult.model_validate(payload)


_REGISTRY = RemoteWorkerRegistry()


def get_remote_worker_registry() -> RemoteWorkerRegistry:
    return _REGISTRY


def reset_remote_worker_registry_for_tests() -> None:
    global _REGISTRY
    _REGISTRY = RemoteWorkerRegistry()
