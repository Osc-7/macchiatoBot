"""
Agent Core 内核协议 — syscall 接口定义与 InternalLoader。

供 ScheduleAgent.run_loop() 使用，供 system.kernel 导入。
"""

from .action import (
    ContextCompressedEvent,
    ContextOverflowAction,
    CoreStatsAction,
    KernelAction,
    KernelEvent,
    KernelRequest,
    KillEvent,
    ReturnAction,
    ToolCallAction,
    ToolResultEvent,
)
from .loader import InternalLoader, LLMPayload
from .profile import CoreProfile

__all__ = [
    # Actions (Core → Kernel)
    "KernelAction",
    "ToolCallAction",
    "ReturnAction",
    "ContextOverflowAction",
    "CoreStatsAction",
    # Events (Kernel → Core)
    "KernelEvent",
    "ToolResultEvent",
    "ContextCompressedEvent",
    "KillEvent",
    # Request (Frontend → Kernel)
    "KernelRequest",
    # Loader
    "InternalLoader",
    "LLMPayload",
    # Profile
    "CoreProfile",
]
