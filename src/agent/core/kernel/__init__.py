"""
Agent Kernel 层。

类比操作系统内核，负责：
- 调度 AgentCore（纯状态机）的执行
- 持有并中介所有 IO 能力（LLM 调用、工具执行）
- 管理 session 生命周期（CorePool）
- 实现输入优先级队列 + 乱序完成路由（KernelScheduler + OutputRouter）
"""

from .action import (
    KernelAction,
    KernelEvent,
    KernelRequest,
    LLMRequestAction,
    LLMResponseEvent,
    ReturnAction,
    ToolCallAction,
    ToolResultEvent,
)
from .core_pool import CorePool
from .kernel import AgentKernel
from .loader import InternalLoader, LLMPayload
from .scheduler import KernelScheduler, OutputRouter

__all__ = [
    # Actions (AgentCore → Kernel)
    "KernelAction",
    "LLMRequestAction",
    "ToolCallAction",
    "ReturnAction",
    # Events (Kernel → AgentCore)
    "KernelEvent",
    "LLMResponseEvent",
    "ToolResultEvent",
    # Request
    "KernelRequest",
    # Core components
    "AgentKernel",
    "InternalLoader",
    "LLMPayload",
    "CorePool",
    "KernelScheduler",
    "OutputRouter",
]
