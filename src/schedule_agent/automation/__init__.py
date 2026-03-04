"""Automation subsystem exports."""

from .agent_task import AgentTask, ContextPolicy, TaskStatus, make_cron_task, make_user_task
from .core_gateway import AutomationCoreGateway, SessionCutPolicy
from .ipc import AutomationIPCClient, AutomationIPCServer, IPCServerPolicy, default_socket_path
from .session_registry import SessionRegistry
from .event_bus import AsyncEventBus
from .runtime import AutomationRuntime, get_runtime, reset_runtime
from .scheduler import AutomationScheduler
from .session_manager import SessionManager
from .services import (
    NormalizationWriteService,
    NotificationOutboxService,
    SummaryService,
    SyncIngestionService,
)
from .task_queue import AgentTaskQueue
from .types import (
    AutomationPolicy,
    Digest,
    ExternalItem,
    JobDefinition,
    JobRun,
    JobStatus,
    NotificationOutbox,
    NotificationStatus,
    SourceAccount,
    SyncCursor,
)

__all__ = [
    # Queue-driven architecture (new)
    "AgentTask",
    "AgentTaskQueue",
    "ContextPolicy",
    "TaskStatus",
    "make_cron_task",
    "make_user_task",
    "SessionManager",
    "AutomationCoreGateway",
    "SessionCutPolicy",
    "AutomationIPCClient",
    "AutomationIPCServer",
    "IPCServerPolicy",
    "default_socket_path",
    "SessionRegistry",
    # Event-driven runtime (existing)
    "AsyncEventBus",
    "AutomationRuntime",
    "get_runtime",
    "reset_runtime",
    "AutomationScheduler",
    "SyncIngestionService",
    "NormalizationWriteService",
    "SummaryService",
    "NotificationOutboxService",
    "AutomationPolicy",
    "Digest",
    "ExternalItem",
    "JobDefinition",
    "JobRun",
    "JobStatus",
    "NotificationOutbox",
    "NotificationStatus",
    "SourceAccount",
    "SyncCursor",
]
