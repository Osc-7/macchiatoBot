"""
核心模块 - 包含 LLM 客户端、工具系统、上下文管理和 Agent
"""

from .agent import ScheduleAgent
from .adapters import ScheduleAgentAdapter
from .context import ConversationContext, TimeContext, get_time_context
from .interfaces import (
    AgentHooks,
    AgentRunInput,
    AgentRunResult,
    AgentSessionState,
    CoreEvent,
    CoreSession,
    ExpireSessionCommand,
    InjectMessageCommand,
    RESERVED_METADATA_KEYS,
    RunTurnCommand,
    merge_run_metadata,
)
from .llm import LLMClient, LLMResponse, ToolCall
from .tools import BaseTool, ToolDefinition, ToolParameter, ToolRegistry, ToolResult

__all__ = [
    # Agent
    "ScheduleAgent",
    "ScheduleAgentAdapter",
    "CoreSession",
    "CoreEvent",
    "AgentHooks",
    "AgentRunInput",
    "AgentRunResult",
    "AgentSessionState",
    "RunTurnCommand",
    "InjectMessageCommand",
    "ExpireSessionCommand",
    "RESERVED_METADATA_KEYS",
    "merge_run_metadata",
    # LLM
    "LLMClient",
    "LLMResponse",
    "ToolCall",
    # Context
    "ConversationContext",
    "TimeContext",
    "get_time_context",
    # Tools
    "BaseTool",
    "ToolDefinition",
    "ToolParameter",
    "ToolRegistry",
    "ToolResult",
]
