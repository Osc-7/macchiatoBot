"""Shared remote workspace protocol models.

This module is intentionally lightweight: it must stay importable by the
standalone ``macchiato-remote`` worker without pulling in the daemon, LLM,
Feishu, memory, or scheduler stacks.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, field_validator

REMOTE_WORKSPACE_MOUNT = "/workspace"

# 与 daemon 协商能力；worker 包主版本 bump 时递增
REMOTE_PROTOCOL_VERSION = 3
REMOTE_WORKER_CAPABILITIES = (
    "exec",
    "file_read",
    "file_write",
    "file_blob_read",
    "reset_shell",
    "shell_capture",
    "job_start",
    "job_status",
    "job_tail",
    "job_stop",
    "mcp_ensure",
    "mcp_list_tools",
    "mcp_call_tool",
    "mcp_shutdown",
)

RemotePermissionProfile = Literal["strict", "dev", "host-user", "host-admin"]
RemoteWorkspaceStatus = Literal["active", "pending", "released", "error"]


class RemoteGrant(BaseModel):
    """A temporary host path grant exposed to a remote workspace session."""

    host_path: str
    access: Literal["read", "write"]
    mount_path: Optional[str] = None
    expires_at: Optional[float] = None


class RemoteWorkspaceState(BaseModel):
    """Daemon-side view of a session currently using a remote workspace."""

    session_id: str
    login: str
    requested_path: str
    resolved_path: Optional[str] = None
    profile: RemotePermissionProfile = "dev"
    status: RemoteWorkspaceStatus = "active"
    workspace_mount: str = REMOTE_WORKSPACE_MOUNT
    device_label: Optional[str] = None
    activated_at: float = Field(default_factory=time.time)
    expires_at: Optional[float] = None
    grants: list[RemoteGrant] = Field(default_factory=list)
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("session_id", "login", "requested_path")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("workspace_mount")
    @classmethod
    def _mount_is_absolute(cls, value: str) -> str:
        value = (value or "").strip() or REMOTE_WORKSPACE_MOUNT
        if not value.startswith("/"):
            raise ValueError("workspace_mount must be absolute")
        return value

    @property
    def display_remote_path(self) -> str:
        return self.resolved_path or self.requested_path

    def is_expired(self, *, now: Optional[float] = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= self.expires_at


class RemoteWorkspaceOpenRequest(BaseModel):
    request_id: str
    session_id: str
    requested_path: str
    profile: RemotePermissionProfile = "dev"


class RemoteWorkspaceOpenResult(BaseModel):
    request_id: str
    session_id: str
    success: bool
    resolved_path: Optional[str] = None
    device_label: Optional[str] = None
    message: str = ""
    error: Optional[str] = None


class RemoteWorkspaceCloseRequest(BaseModel):
    request_id: str
    session_id: str


class RemoteWorkspaceCloseResult(BaseModel):
    request_id: str
    session_id: str
    success: bool
    message: str = ""
    error: Optional[str] = None


class RemoteCommandRequest(BaseModel):
    request_id: str
    session_id: str
    command: str
    cwd: str = REMOTE_WORKSPACE_MOUNT
    timeout_seconds: Optional[float] = None
    wait_window_ms: Optional[int] = None
    wait_for_completion: bool = False
    output_limit: Optional[int] = None
    extra_read_roots: list[str] = Field(default_factory=list)


class RemoteCommandResult(BaseModel):
    request_id: str
    command: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    timed_out: bool = False
    truncated: bool = False
    cwd: str = REMOTE_WORKSPACE_MOUNT
    error: Optional[str] = None
    backgrounded: bool = False
    job_id: Optional[str] = None
    job_status: Optional[str] = None
    job_log_path: Optional[str] = None
    job_pid: Optional[int] = None


class RemoteFileReadRequest(BaseModel):
    request_id: str
    session_id: str
    path: str
    encoding: str = "utf-8"
    start_line: Optional[int] = None
    end_line: Optional[int] = None


class RemoteFileReadResult(BaseModel):
    request_id: str
    path: str
    content: str
    encoding: str = "utf-8"
    truncated: bool = False
    error: Optional[str] = None


class RemoteFileWriteRequest(BaseModel):
    request_id: str
    session_id: str
    path: str
    content: str
    encoding: str = "utf-8"
    mode: Literal["overwrite", "append"] = "overwrite"


class RemoteFileWriteResult(BaseModel):
    request_id: str
    path: str
    bytes_written: int = 0
    encoding: str = "utf-8"
    error: Optional[str] = None


class RemoteFileBlobReadRequest(BaseModel):
    request_id: str
    session_id: str
    path: str
    max_bytes: int = 20 * 1024 * 1024


class RemoteFileBlobReadResult(BaseModel):
    request_id: str
    path: str
    content_base64: str = ""
    file_name: str = ""
    mime_type: str = "application/octet-stream"
    bytes_read: int = 0
    truncated: bool = False
    error: Optional[str] = None


class RemoteShellResetRequest(BaseModel):
    request_id: str
    session_id: str


class RemoteShellResetResult(BaseModel):
    request_id: str
    session_id: str
    success: bool
    message: str = ""
    error: Optional[str] = None


class RemoteShellCaptureRequest(BaseModel):
    request_id: str
    session_id: str


class RemoteShellCaptureResult(BaseModel):
    request_id: str
    session_id: str
    cwd: str = ""
    env: Dict[str, str] = Field(default_factory=dict)
    error: Optional[str] = None


# ── Job lifecycle ──────────────────────────────────────────

class RemoteJobStartRequest(BaseModel):
    request_id: str
    session_id: str
    command: str
    cwd: str = REMOTE_WORKSPACE_MOUNT
    timeout_seconds: Optional[float] = None
    env: Dict[str, str] = Field(default_factory=dict)


class RemoteJobStartResult(BaseModel):
    request_id: str
    session_id: str
    job_id: str
    pid: Optional[int] = None
    log_path: str = ""
    status: str = "running"
    error: Optional[str] = None


class RemoteJobStatusRequest(BaseModel):
    request_id: str
    session_id: str
    job_id: str


class RemoteJobStatusResult(BaseModel):
    request_id: str
    session_id: str
    job_id: str
    status: str
    command: str = ""
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    timed_out: bool = False
    duration_seconds: float = 0.0
    log_path: str = ""
    error: Optional[str] = None


class RemoteJobTailRequest(BaseModel):
    request_id: str
    session_id: str
    job_id: str
    lines: int = 200
    offset: int = 0


class RemoteJobTailResult(BaseModel):
    request_id: str
    session_id: str
    job_id: str
    status: str
    total_lines: int = 0
    read_lines: int = 0
    offset: int = 0
    log_path: str = ""
    head_lines: list[str] = Field(default_factory=list)
    tail_lines: list[str] = Field(default_factory=list)
    error: Optional[str] = None


class RemoteJobStopRequest(BaseModel):
    request_id: str
    session_id: str
    job_id: str
    signal: str = "SIGTERM"


class RemoteJobStopResult(BaseModel):
    request_id: str
    session_id: str
    job_id: str
    success: bool
    error: Optional[str] = None


# ── MCP (protocol v3) ──────────────────────────────────────


class RemoteMcpServerRef(BaseModel):
    name: str


class RemoteMcpEnsureRequest(BaseModel):
    request_id: str
    session_id: str
    servers: list[RemoteMcpServerRef] = Field(default_factory=list)


class RemoteMcpServerStatus(BaseModel):
    name: str
    ok: bool
    error: Optional[str] = None


class RemoteMcpEnsureResult(BaseModel):
    request_id: str
    servers: list[RemoteMcpServerStatus] = Field(default_factory=list)


class RemoteMcpToolMeta(BaseModel):
    name: str
    description: str = ""
    input_schema: Dict[str, Any] = Field(default_factory=dict)


class RemoteMcpListToolsRequest(BaseModel):
    request_id: str
    session_id: str
    server_name: str
    refresh: bool = False


class RemoteMcpListToolsResult(BaseModel):
    request_id: str
    server_name: str
    tools: list[RemoteMcpToolMeta] = Field(default_factory=list)
    error: Optional[str] = None


class RemoteMcpCallToolRequest(BaseModel):
    request_id: str
    session_id: str
    server_name: str
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: Optional[float] = None


class RemoteMcpCallToolResult(BaseModel):
    request_id: str
    is_error: bool = False
    content: list[Dict[str, Any]] = Field(default_factory=list)
    structured_content: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class RemoteMcpShutdownRequest(BaseModel):
    request_id: str
    session_id: str
    server_name: Optional[str] = None


class RemoteMcpShutdownResult(BaseModel):
    request_id: str
    closed: list[str] = Field(default_factory=list)
    error: Optional[str] = None
