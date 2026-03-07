"""Agent Core 抽象层共享数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .events import CoreEvent


@dataclass(frozen=True)
class AgentRunInput:
    """单轮输入。"""

    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRunResult:
    """单轮输出。"""

    output_text: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    """本轮回复要附带发给用户的附件，如 [{"type": "image", "path": "..."}] 或 {"type": "image", "url": "..."}"""


@dataclass(frozen=True)
class AgentSessionState:
    """会话状态快照。"""

    session_id: str
    turn_count: int
    token_usage: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentHooks:
    """统一的回调钩子。"""

    on_assistant_delta: Optional[Callable[[str], Any]] = None
    on_reasoning_delta: Optional[Callable[[str], Any]] = None
    on_trace_event: Optional[Callable[[Dict[str, Any]], Any]] = None
    on_event: Optional[Callable[[CoreEvent], Any]] = None
