"""
Kernel 层系统调用类型定义。

类比操作系统的 syscall 接口：
- AgentCore 直接持有 LLMClient，在内部自旋完成多轮 LLM 推理（类比 CPU 自主执行）
- 向 Kernel 发出系统调用的时机：
    1. ToolCallAction        — 外部 IO（工具执行），Kernel 是唯一有权调用外部工具的实体
    2. ReturnAction          — 进程退出，通知 Kernel 本轮处理完成，交还控制权
    3. ContextOverflowAction — 上下文窗口达到压缩阈值，请求 Kernel 暂停并压缩后恢复
    4. CoreStatsAction       — Core 被 kill 前上报资源统计，供 Kernel 调摘要器和计费

Kernel 向 AgentCore 回传的事件（syscall 返回值）：
    1. ToolResultEvent       — 工具执行结果
    2. ContextCompressedEvent — 上下文压缩完成，Core 可恢复执行
    3. KillEvent             — Kernel 要求 Core 优雅关闭

同时定义 KernelRequest：前端层向 Kernel 提交的输入请求。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Union

if TYPE_CHECKING:
    from agent_core.tools import ToolResult


# ---------------------------------------------------------------------------
# KernelAction — AgentCore 向 Kernel 发起的"系统调用意图"
# ---------------------------------------------------------------------------


@dataclass
class ToolCallAction:
    """AgentCore 发出 IO 系统调用：执行某个外部工具。

    Kernel 收到后调用 ToolRegistry.execute()，将结果通过
    ToolResultEvent asend 回 AgentCore。
    """

    type: Literal["tool_call"] = field(default="tool_call", init=False)
    tool_call_id: str = ""
    tool_name: str = ""
    arguments: Any = None


@dataclass
class ReturnAction:
    """AgentCore 宣告本轮处理完成，交还控制权（进程退出）。

    Kernel 收到后终止驱动循环，将结果路由回前端。
    """

    type: Literal["return"] = field(default="return", init=False)
    message: str = ""
    status: str = "completed"  # completed | error | overflow | fallback
    attachments: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ContextOverflowAction:
    """Core 上下文窗口达到压缩阈值，请求 Kernel 暂停并压缩。

    约定：只在完整 thought→tools→observations 循环结束后发出，
    不在 tool_result 尚未全部收集期间触发，避免截断中途工具调用链。

    Kernel 收到后执行压缩，完成后以 ContextCompressedEvent 通知 Core 恢复。
    """

    type: Literal["context_overflow"] = field(default="context_overflow", init=False)
    current_tokens: int = 0
    threshold_tokens: int = 0
    session_id: str = ""


@dataclass
class CoreStatsAction:
    """Core 被 kill 前上报本次 session 的资源统计。

    Kernel 拿到后调用摘要器生成 session 摘要并写入长期记忆，
    再完成进程回收。
    """

    type: Literal["core_stats"] = field(default="core_stats", init=False)
    token_usage: Dict[str, int] = field(default_factory=dict)
    session_start_time: str = ""
    turn_count: int = 0
    session_id: str = ""


KernelAction = Union[
    ToolCallAction, ReturnAction, ContextOverflowAction, CoreStatsAction
]


# ---------------------------------------------------------------------------
# AgentMessage — Agent 间通信协议信封
# ---------------------------------------------------------------------------


@dataclass
class AgentMessage:
    """Agent 间 P2P 消息信封，附加在 KernelRequest.metadata[METADATA_KEY_AGENT_MESSAGE] 中。

    键名见 `system.multi_agent.constants.METADATA_KEY_AGENT_MESSAGE`（值为 "_agent_message"）。

    用途：
    - create_subagent / create_parallel_subagents 完成后由 CorePool 注入
    - send_message_to_agent / reply_to_message 实现 P2P 双向通信
    - inject_turn() 携带此信封，使接收方 agent 能感知消息来源与类型

    与 A2A 协议对应关系（便于日后接 A2A 网关）：
    - message_id       ↔ A2A Task.id / Message.id
    - sender_session   ↔ A2A client agent endpoint
    - receiver_session ↔ A2A server agent endpoint
    - correlation_id   ↔ A2A contextId（回复链关联）
    - subagent_id      ↔ A2A Task.id（子任务追踪）
    """

    message_id: str                  # 唯一 ID（uuid）
    sender_session: str              # 发送方 session_id
    receiver_session: str            # 接收方 session_id
    message_type: Literal[
        "task",    # subagent 任务完成通知（subagent → parent）
        "reply",   # 回复某条 query 消息
        "notify",  # 单向通知（无需回复）
        "query",   # 需要对方 reply_to_message 回复
    ] = "notify"
    correlation_id: Optional[str] = None   # reply 时填入原 message_id
    subagent_id: Optional[str] = None      # task/reply 类型时记录对应子任务 ID
    require_reply: bool = False


# ---------------------------------------------------------------------------
# KernelEvent — Kernel 向 AgentCore 回传的事件（syscall 返回值）
# ---------------------------------------------------------------------------


@dataclass
class ToolResultEvent:
    """Kernel 执行了工具调用后，将结果回传给 AgentCore。"""

    tool_call_id: str
    result: "ToolResult"


@dataclass
class ContextCompressedEvent:
    """Kernel 完成上下文压缩后通知 Core 恢复执行。

    compressed_summary 是被压缩掉的旧消息的摘要文本，
    Core 可将其注入 working_memory 或系统提示以保留语义连续性。
    messages_kept 是压缩后保留的完整轮次数量（供 Core 更新内部计数）。
    """

    compressed_summary: str = ""
    messages_kept: int = 0


@dataclass
class KillEvent:
    """Kernel 要求 Core 优雅关闭。

    Core 收到后应 yield CoreStatsAction 完成资源上报，然后退出 run_loop_kill()。
    reason 枚举：session_expired | manual | system_shutdown
    """

    reason: str = "session_expired"


KernelEvent = Union[ToolResultEvent, ContextCompressedEvent, KillEvent]


# ---------------------------------------------------------------------------
# KernelRequest — 前端层向 Kernel 提交的输入请求
# ---------------------------------------------------------------------------


@dataclass(order=True)
class KernelRequest:
    """前端提交给 KernelScheduler 的输入请求。

    按 (priority, enqueued_at) 排序：
    - priority 越小越优先（0=normal, -1=high）
    - 同优先级内按入队时间 FIFO

    request_id 用于 OutputBus 精准回传结果（乱序完成 + 正确路由）。
    profile 字段可选，前端/automation 创建请求时传入，覆盖 CorePool 默认值。
    """

    priority: int = 0
    enqueued_at: float = field(default_factory=time.monotonic)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # 以下字段不参与比较排序
    frontend_id: str = field(default="cli", compare=False)
    session_id: str = field(default="", compare=False)
    text: str = field(default="", compare=False)
    metadata: Dict[str, Any] = field(default_factory=dict, compare=False)
    profile: Optional["CoreProfile"] = field(default=None, compare=False)  # noqa: F821

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
        profile: Optional["CoreProfile"] = None,  # noqa: F821
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
            profile=profile,
        )
