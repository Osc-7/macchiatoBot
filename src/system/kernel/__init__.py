"""
System Kernel — 纯 IO 调度器（工具执行 + 生命周期管理）。

类比操作系统内核，负责：
- AgentKernel：执行 ToolCallAction，驱动 AgentCore，kill Core 并收集 CoreStats
- CorePool：PCB 池，session → CoreEntry（AgentCore + CoreProfile + TTL 元数据）
- KernelScheduler + OutputBus：输入队列 + 输出总线 + TTL 扫描循环
- SessionSummarizer：kill 后调用，生成 session 摘要写入长期记忆

协议类型（ToolCallAction, ReturnAction, CoreProfile 等）定义在 agent_core.kernel_interface。
"""

from agent_core.kernel_interface import (
    ContextCompressedEvent,
    ContextOverflowAction,
    CoreProfile,
    CoreStatsAction,
    KernelAction,
    KernelEvent,
    KernelRequest,
    KillEvent,
    ReturnAction,
    ToolCallAction,
    ToolResultEvent,
    InternalLoader,
    LLMPayload,
)
from .core_pool import CoreEntry, CorePool
from .kernel import AgentKernel
from .scheduler import KernelScheduler, OutputBus
from .subagent_registry import SubagentInfo, SubagentRegistry
from .summarizer import SessionSummarizer

__all__ = [
    # Kernel 协议类型
    "KernelAction",
    "KernelEvent",
    "KernelRequest",
    "ToolCallAction",
    "ReturnAction",
    "ContextOverflowAction",
    "CoreStatsAction",
    "ToolResultEvent",
    "ContextCompressedEvent",
    "KillEvent",
    "CoreProfile",
    "InternalLoader",
    "LLMPayload",
    # Kernel 核心组件
    "AgentKernel",
    "CoreEntry",
    "CorePool",
    "KernelScheduler",
    "OutputBus",
    "SessionSummarizer",
    "SubagentInfo",
    "SubagentRegistry",
]
