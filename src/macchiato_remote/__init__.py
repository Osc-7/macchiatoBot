"""Lightweight remote-worker package for macchiatoBot."""

from .protocol import (
    REMOTE_WORKSPACE_MOUNT,
    RemoteCommandRequest,
    RemoteCommandResult,
    RemoteFileReadRequest,
    RemoteFileReadResult,
    RemoteFileWriteRequest,
    RemoteFileWriteResult,
    RemoteGrant,
    RemotePermissionProfile,
    RemoteWorkspaceCloseRequest,
    RemoteWorkspaceCloseResult,
    RemoteWorkspaceOpenRequest,
    RemoteWorkspaceOpenResult,
    RemoteWorkspaceState,
    RemoteWorkspaceStatus,
)

__all__ = [
    "REMOTE_WORKSPACE_MOUNT",
    "RemoteCommandRequest",
    "RemoteCommandResult",
    "RemoteFileReadRequest",
    "RemoteFileReadResult",
    "RemoteFileWriteRequest",
    "RemoteFileWriteResult",
    "RemoteGrant",
    "RemotePermissionProfile",
    "RemoteWorkspaceCloseRequest",
    "RemoteWorkspaceCloseResult",
    "RemoteWorkspaceOpenRequest",
    "RemoteWorkspaceOpenResult",
    "RemoteWorkspaceState",
    "RemoteWorkspaceStatus",
]
