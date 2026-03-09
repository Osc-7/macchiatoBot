"""
Kernel 层系统调用类型定义。

类比操作系统的 syscall 接口：
- AgentCore（纯状态机）通过 yield KernelAction 向 Kernel 声明需要什么 IO
- Kernel 执行后，通过 asend(KernelEvent) 将结果回传给 AgentCore

同时定义 KernelRequest：前端层向 Kernel 提交的输入请求。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Union

if TYPE_CHECKING:
    from agent.core.llm import LLMResponse
    from agent.core.tools import ToolResult


# ---------------------------------------------------------------------------
# KernelAction — AgentCore 向 Kernel 发起的"系统调用意图"
# ---------------------------------------------------------------------------


@dataclass
class LLMRequestAction:
    """AgentCore 声明：我需要一次 LLM 思考。

    Kernel 收到后组装 Prompt/Context，调用 LLMClient，将结果通过
    LLMResponseEvent asend 回 AgentCore。
    """

    type: Literal["llm_request"] = field(default="llm_request", init=False)


@dataclass
class ToolCallAction:
    """AgentCore 声明：我需要执行某个工具。

    Kernel 收到后调用 ToolRegistry.execute()，将结果通过
    ToolResultEvent asend 回 AgentCore。
    """

    type: Literal["tool_call"] = field(default="tool_call", init=False)
    tool_call_id: str = ""
    tool_name: str = ""
    arguments: Any = None


@dataclass
class ReturnAction:
    """AgentCore 宣告本轮处理完成，返回最终结果。

    Kernel 收到后终止驱动循环，将结果路由回前端。
    """

    type: Literal["return"] = field(default="return", init=False)
    message: str = ""
    status: str = "completed"  # completed | error | overflow
    attachments: List[Dict[str, Any]] = field(default_factory=list)


KernelAction = Union[LLMRequestAction, ToolCallAction, ReturnAction]


# ---------------------------------------------------------------------------
# KernelEvent — Kernel 向 AgentCore 回传的事件（syscall 返回值）
# ---------------------------------------------------------------------------


@dataclass
class LLMResponseEvent:
    """Kernel 执行了 LLM 调用后，将结果回传给 AgentCore。"""

    response: "LLMResponse"


@dataclass
class ToolResultEvent:
    """Kernel 执行了工具调用后，将结果回传给 AgentCore。"""

    tool_call_id: str
    result: "ToolResult"


KernelEvent = Union[LLMResponseEvent, ToolResultEvent]


# ---------------------------------------------------------------------------
# KernelRequest — 前端层向 Kernel 提交的输入请求
# ---------------------------------------------------------------------------


@dataclass(order=True)
class KernelRequest:
    """前端提交给 KernelScheduler 的输入请求。

    按 (priority, enqueued_at) 排序：
    - priority 越小越优先（0=normal, -1=high）
    - 同优先级内按入队时间 FIFO

    request_id 用于 OutputRouter 精准回传结果（乱序完成 + 正确路由）。
    """

    priority: int = 0
    enqueued_at: float = field(default_factory=time.monotonic)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # 以下字段不参与比较排序
    frontend_id: str = field(default="cli", compare=False)
    session_id: str = field(default="", compare=False)
    text: str = field(default="", compare=False)
    metadata: Dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def create(
        cls,
        text: str,
        session_id: str,
        *,
        frontend_id: str = "cli",
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
    ) -> "KernelRequest":
        """便捷工厂方法，自动填充 enqueued_at 和 request_id。"""
        return cls(
            priority=priority,
            enqueued_at=time.monotonic(),
            request_id=request_id or str(uuid.uuid4()),
            frontend_id=frontend_id,
            session_id=session_id,
            text=text,
            metadata=metadata or {},
        )
