"""
System 层 — 调度、会话管理、内核执行。

包含：
- automation：会话管理、IPC、任务队列、CoreGateway
- kernel：AgentKernel、CorePool、KernelScheduler、OutputBus
- multi_agent：P2P 投递标签与 AgentMessage 元数据键（与 Kernel 同进程的原生多会话协作）
"""

from .automation import (
    AgentTask,
    AgentTaskQueue,
    AutomationCoreGateway,
    AutomationIPCClient,
    AutomationIPCServer,
    AutomationRuntime,
    AutomationScheduler,
    IPCServerPolicy,
    SessionCutPolicy,
    SessionManager,
    SessionRegistry,
    default_socket_path,
    get_runtime,
    reset_runtime,
)
from .kernel import (
    AgentKernel,
    CorePool,
    KernelRequest,
    KernelScheduler,
    OutputBus,
)

__all__ = [
    "AgentKernel",
    "CorePool",
    "KernelRequest",
    "KernelScheduler",
    "OutputBus",
    "AgentTask",
    "AgentTaskQueue",
    "AutomationCoreGateway",
    "AutomationIPCClient",
    "AutomationIPCServer",
    "AutomationRuntime",
    "AutomationScheduler",
    "IPCServerPolicy",
    "SessionCutPolicy",
    "SessionManager",
    "SessionRegistry",
    "default_socket_path",
    "get_runtime",
    "reset_runtime",
]
