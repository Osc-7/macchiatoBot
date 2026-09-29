"""In-process registry for connected remote workers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from macchiato_remote.protocol import (
    REMOTE_BLOB_CHUNK_BYTES,
    REMOTE_BLOB_IDLE_TIMEOUT_SECONDS,
    REMOTE_BLOB_MAX_BYTES,
    REMOTE_BLOB_STREAM_MAX_BYTES,
    REMOTE_BLOB_TRANSFER_TIMEOUT_SECONDS,
    REMOTE_WORKSPACE_MOUNT,
    RemoteBlobPullRequest,
    RemoteBlobPushBeginRequest,
    RemoteBlobPushEndRequest,
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
    encode_file_too_large,
    normalize_blob_request_id,
    pack_blob_chunk,
    unpack_blob_chunk,
)

SendJson = Callable[[Dict[str, Any]], Awaitable[None]]
SendBytes = Callable[[bytes], Awaitable[None]]


@dataclass
class BlobPullOutcome:
    dest_path: Optional[str] = None
    file_name: str = ""
    mime_type: str = "application/octet-stream"
    bytes_read: int = 0
    sha256: str = ""
    error: Optional[str] = None
    truncated: bool = False


@dataclass
class BlobPushOutcome:
    path: str = ""
    bytes_written: int = 0
    error: Optional[str] = None


class _PullSession:
    def __init__(self, *, request_id: str, dest_path: Path) -> None:
        self.request_id = request_id
        self.dest_path = dest_path
        self.tmp_path = dest_path.with_name(dest_path.name + f".{request_id[:8]}.part")
        self.hasher = hashlib.sha256()
        self.next_seq = 0
        self.written = 0
        self.expected_size: Optional[int] = None
        self.file_name = dest_path.name
        self.mime_type = "application/octet-stream"
        self.handle: Any = None
        self.last_activity = time.monotonic()
        loop = asyncio.get_running_loop()
        self.done: asyncio.Future[BlobPullOutcome] = loop.create_future()
        self._watchdog: Optional[asyncio.Task[None]] = None

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def start_watchdog(self) -> None:
        self._watchdog = asyncio.create_task(self._idle_watch())

    async def _idle_watch(self) -> None:
        timeout = float(REMOTE_BLOB_IDLE_TIMEOUT_SECONDS)
        while not self.done.done():
            await asyncio.sleep(min(5.0, timeout / 2))
            if self.done.done():
                return
            if time.monotonic() - self.last_activity > timeout:
                self.fail(TimeoutError("blob stream idle timeout"))
                return

    def fail(self, exc: BaseException) -> None:
        self._close_tmp(unlink=True)
        if not self.done.done():
            if isinstance(exc, Exception):
                self.done.set_result(
                    BlobPullOutcome(file_name=self.file_name, error=str(exc))
                )
            else:
                self.done.set_exception(exc)
        if self._watchdog is not None:
            self._watchdog.cancel()

    def _close_tmp(self, *, unlink: bool) -> None:
        try:
            if self.handle is not None:
                self.handle.close()
        except Exception:
            pass
        self.handle = None
        if unlink:
            try:
                self.tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def on_begin(self, payload: Dict[str, Any]) -> None:
        self.touch()
        err = payload.get("error")
        if err:
            self.fail(RuntimeError(str(err)))
            return
        self.expected_size = int(payload.get("size") or 0)
        self.file_name = str(payload.get("file_name") or self.file_name)
        self.mime_type = str(payload.get("mime_type") or self.mime_type)
        try:
            self.dest_path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = self.tmp_path.open("wb")
        except OSError as exc:
            self.fail(exc)

    def on_chunk(self, seq: int, payload: bytes) -> None:
        self.touch()
        if self.handle is None:
            self.fail(RuntimeError("BLOB_BEGIN_MISSING"))
            return
        if seq != self.next_seq:
            self.fail(RuntimeError(f"BLOB_SEQ_GAP: expected {self.next_seq} got {seq}"))
            return
        self.handle.write(payload)
        self.hasher.update(payload)
        self.written += len(payload)
        self.next_seq += 1

    def on_end(self, payload: Dict[str, Any]) -> None:
        self.touch()
        err = payload.get("error")
        if err:
            self.fail(RuntimeError(str(err)))
            return
        if self.handle is None and self.expected_size not in (None, 0):
            self.fail(RuntimeError("BLOB_BEGIN_MISSING"))
            return
        try:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
        except Exception:
            pass
        actual = self.hasher.hexdigest()
        expected = str(payload.get("sha256") or "").strip().lower()
        total = int(payload.get("total_bytes") or self.written)
        if expected and actual != expected:
            self.fail(RuntimeError(f"BLOB_SHA256_MISMATCH: {actual} != {expected}"))
            return
        if self.expected_size is not None and self.written != self.expected_size:
            self.fail(
                RuntimeError(
                    f"BLOB_SIZE_MISMATCH: {self.written} != {self.expected_size}"
                )
            )
            return
        try:
            self.tmp_path.replace(self.dest_path)
        except OSError as exc:
            self.fail(exc)
            return
        if not self.done.done():
            self.done.set_result(
                BlobPullOutcome(
                    dest_path=str(self.dest_path),
                    file_name=str(payload.get("file_name") or self.file_name),
                    mime_type=str(payload.get("mime_type") or self.mime_type),
                    bytes_read=total,
                    sha256=actual,
                )
            )
        if self._watchdog is not None:
            self._watchdog.cancel()


@dataclass
class RemoteWorkerConnection:
    """A live worker connection owned by the daemon process."""

    login: str
    send_json: SendJson
    send_bytes: Optional[SendBytes] = None
    pending: Dict[str, asyncio.Future[Dict[str, Any]]] = field(default_factory=dict)
    stream_sessions: Dict[str, _PullSession] = field(default_factory=dict)
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
        padded = normalize_blob_request_id(request_id)
        session = self.stream_sessions.get(request_id) or self.stream_sessions.get(
            padded
        )
        msg_type = str(message.get("type") or "")
        if session is not None and msg_type in {"blob_begin", "blob_end"}:
            if msg_type == "blob_begin":
                session.on_begin(payload)
            else:
                session.on_end(payload)
            return
        fut = self.pending.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(payload)

    def handle_binary(self, frame: bytes) -> None:
        try:
            request_id, seq, payload = unpack_blob_chunk(frame)
        except ValueError:
            return
        session = self.stream_sessions.get(request_id) or self.stream_sessions.get(
            normalize_blob_request_id(request_id)
        )
        if session is None:
            return
        session.on_chunk(seq, payload)

    def fail_pending(self, exc: BaseException) -> None:
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self.pending.clear()
        for session in list(self.stream_sessions.values()):
            session.fail(exc)
        self.stream_sessions.clear()


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

    def is_connected(self, login: str) -> bool:
        """同步查询 login 是否有在线 WS 连接（供 status 之类的同步路径使用）。"""
        key = (login or "").strip()
        return bool(key) and key in self._connections

    def connected_snapshots(self) -> list[dict[str, str]]:
        """在线 worker 的 login / 设备 / 启动目录快照。不含 token。"""
        rows: list[dict[str, str]] = []
        for login, conn in list(self._connections.items()):
            meta = dict(getattr(conn, "hello_meta", None) or {})
            rows.append(
                {
                    "login": login,
                    "device_label": str(meta.get("device_label") or ""),
                    "default_workspace": str(meta.get("default_workspace") or ""),
                    "os": str(meta.get("os") or ""),
                }
            )
        rows.sort(key=lambda row: row["login"])
        return rows

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
        timeout_seconds: float = REMOTE_BLOB_TRANSFER_TIMEOUT_SECONDS,
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
        timeout_seconds: float = REMOTE_BLOB_TRANSFER_TIMEOUT_SECONDS,
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

    def worker_supports_blob_stream(self, login: str) -> bool:
        key = (login or "").strip()
        conn = self._connections.get(key)
        if conn is None:
            return False
        caps = set(conn.hello_meta.get("capabilities") or [])
        return "blob_stream" in caps

    async def blob_pull_to_path(
        self,
        *,
        login: str,
        session_id: str,
        path: str,
        dest_path: str | Path,
        max_bytes: Optional[int] = None,
    ) -> BlobPullOutcome:
        dest = Path(dest_path)
        if self.worker_supports_blob_stream(login):
            return await self._blob_pull_stream(
                login=login,
                session_id=session_id,
                path=path,
                dest=dest,
                max_bytes=max_bytes,
            )
        return await self._blob_pull_base64(
            login=login,
            session_id=session_id,
            path=path,
            dest=dest,
            max_bytes=max_bytes,
        )

    async def _blob_pull_stream(
        self,
        *,
        login: str,
        session_id: str,
        path: str,
        dest: Path,
        max_bytes: Optional[int],
    ) -> BlobPullOutcome:
        conn = await self.require(login)
        limit = min(
            max(1, int(max_bytes or REMOTE_BLOB_STREAM_MAX_BYTES)),
            int(REMOTE_BLOB_STREAM_MAX_BYTES),
        )
        request_id = uuid.uuid4().hex
        session = _PullSession(request_id=request_id, dest_path=dest)
        conn.stream_sessions[request_id] = session
        conn.stream_sessions[normalize_blob_request_id(request_id)] = session
        session.start_watchdog()
        req = RemoteBlobPullRequest(
            request_id=request_id,
            session_id=session_id,
            path=path,
            max_bytes=limit,
        )
        try:
            await conn.send_json({"type": "blob_pull", "request": req.model_dump()})
            return await session.done
        except Exception as exc:  # noqa: BLE001
            session.fail(exc if isinstance(exc, BaseException) else RuntimeError(exc))
            if session.done.done():
                return session.done.result()
            return BlobPullOutcome(error=str(exc))
        finally:
            conn.stream_sessions.pop(request_id, None)
            conn.stream_sessions.pop(normalize_blob_request_id(request_id), None)
            if session._watchdog is not None:
                session._watchdog.cancel()

    async def _blob_pull_base64(
        self,
        *,
        login: str,
        session_id: str,
        path: str,
        dest: Path,
        max_bytes: Optional[int],
    ) -> BlobPullOutcome:
        limit = min(
            max(1, int(max_bytes or REMOTE_BLOB_MAX_BYTES)),
            int(REMOTE_BLOB_MAX_BYTES),
        )
        result = await self.file_blob_read(
            login=login,
            session_id=session_id,
            path=path,
            max_bytes=limit,
        )
        if result.error:
            return BlobPullOutcome(
                file_name=result.file_name,
                mime_type=result.mime_type,
                bytes_read=result.bytes_read,
                error=result.error,
                truncated=bool(result.truncated),
            )
        if result.truncated:
            return BlobPullOutcome(
                file_name=result.file_name,
                mime_type=result.mime_type,
                bytes_read=result.bytes_read,
                error=encode_file_too_large(result.bytes_read or limit, limit),
                truncated=True,
            )
        try:
            raw = base64.b64decode(result.content_base64 or "", validate=False)
        except Exception as exc:  # noqa: BLE001
            return BlobPullOutcome(error=f"INVALID_BASE64: {exc}")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        except OSError as exc:
            return BlobPullOutcome(error=str(exc))
        return BlobPullOutcome(
            dest_path=str(dest),
            file_name=result.file_name or dest.name,
            mime_type=result.mime_type or "application/octet-stream",
            bytes_read=len(raw),
        )

    async def blob_push_from_path(
        self,
        *,
        login: str,
        session_id: str,
        src_path: str | Path,
        dest_path: str,
        mode: str = "overwrite",
        max_bytes: Optional[int] = None,
    ) -> BlobPushOutcome:
        src = Path(src_path)
        if self.worker_supports_blob_stream(login):
            return await self._blob_push_stream(
                login=login,
                session_id=session_id,
                src=src,
                dest_path=dest_path,
                mode=mode,
                max_bytes=max_bytes,
            )
        return await self._blob_push_base64(
            login=login,
            session_id=session_id,
            src=src,
            dest_path=dest_path,
            mode=mode,
            max_bytes=max_bytes,
        )

    async def _blob_push_stream(
        self,
        *,
        login: str,
        session_id: str,
        src: Path,
        dest_path: str,
        mode: str,
        max_bytes: Optional[int],
    ) -> BlobPushOutcome:
        conn = await self.require(login)
        if conn.send_bytes is None:
            return await self._blob_push_base64(
                login=login,
                session_id=session_id,
                src=src,
                dest_path=dest_path,
                mode=mode,
                max_bytes=max_bytes,
            )
        try:
            size = src.stat().st_size
        except OSError as exc:
            return BlobPushOutcome(path=dest_path, error=str(exc))
        limit = min(
            max(1, int(max_bytes or REMOTE_BLOB_STREAM_MAX_BYTES)),
            int(REMOTE_BLOB_STREAM_MAX_BYTES),
        )
        if size > limit:
            return BlobPushOutcome(
                path=dest_path, error=encode_file_too_large(size, limit)
            )
        request_id = uuid.uuid4().hex
        begin = RemoteBlobPushBeginRequest(
            request_id=request_id,
            session_id=session_id,
            path=dest_path,
            size=size,
            mode="append" if mode == "append" else "overwrite",
            max_bytes=limit,
            file_name=src.name,
        )
        ready = await conn.request(
            "blob_push_begin",
            begin.model_dump(),
            timeout_seconds=float(REMOTE_BLOB_IDLE_TIMEOUT_SECONDS),
        )
        if ready.get("error"):
            return BlobPushOutcome(path=dest_path, error=str(ready.get("error")))
        hasher = hashlib.sha256()
        seq = 0
        try:
            with src.open("rb") as handle:
                while True:
                    chunk = handle.read(REMOTE_BLOB_CHUNK_BYTES)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    await conn.send_bytes(pack_blob_chunk(request_id, seq, chunk))
                    seq += 1
        except OSError as exc:
            return BlobPushOutcome(path=dest_path, error=str(exc))
        end = RemoteBlobPushEndRequest(
            request_id=request_id,
            sha256=hasher.hexdigest(),
            total_bytes=size,
        )
        result = await conn.request(
            "blob_push_end",
            end.model_dump(),
            timeout_seconds=float(REMOTE_BLOB_IDLE_TIMEOUT_SECONDS),
        )
        return BlobPushOutcome(
            path=str(result.get("path") or dest_path),
            bytes_written=int(result.get("bytes_written") or 0),
            error=result.get("error"),
        )

    async def _blob_push_base64(
        self,
        *,
        login: str,
        session_id: str,
        src: Path,
        dest_path: str,
        mode: str,
        max_bytes: Optional[int],
    ) -> BlobPushOutcome:
        limit = min(
            max(1, int(max_bytes or REMOTE_BLOB_MAX_BYTES)),
            int(REMOTE_BLOB_MAX_BYTES),
        )
        try:
            size = src.stat().st_size
        except OSError as exc:
            return BlobPushOutcome(path=dest_path, error=str(exc))
        if size > limit:
            return BlobPushOutcome(
                path=dest_path, error=encode_file_too_large(size, limit)
            )
        try:
            raw = src.read_bytes()
        except OSError as exc:
            return BlobPushOutcome(path=dest_path, error=str(exc))
        result = await self.file_blob_write(
            login=login,
            session_id=session_id,
            path=dest_path,
            content_base64=base64.b64encode(raw).decode("ascii"),
            mode=mode,
            max_bytes=limit,
        )
        return BlobPushOutcome(
            path=result.path,
            bytes_written=result.bytes_written,
            error=result.error,
        )

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
