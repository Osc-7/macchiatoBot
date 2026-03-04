"""Automation -> Core 命令模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from .models import AgentRunInput

# 统一约定：metadata 保留字段（由框架层写入/维护）
RESERVED_METADATA_KEYS = frozenset(
    {
        "session_id",
        "channel",
        "source",
        "request_id",
    }
)


def merge_run_metadata(
    *,
    session_id: str,
    input_metadata: Dict[str, Any],
    command_metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """
    合并输入与命令 metadata，输出 Core 统一 metadata。

    规则：
    1. 先保留 input_metadata
    2. command_metadata 覆盖同名字段
    3. session_id 以路由 command 为准（不可被覆盖）
    """
    merged = dict(input_metadata)
    merged.update(command_metadata)
    merged["session_id"] = session_id
    return merged


@dataclass(frozen=True)
class RunTurnCommand:
    """执行普通输入轮次。"""

    session_id: str
    input: AgentRunInput
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InjectMessageCommand:
    """从外部通道注入消息。"""

    session_id: str
    input: AgentRunInput
    source: str = "external"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExpireSessionCommand:
    """触发会话切分指令。"""

    session_id: str
    reason: str = "session_expire"
    metadata: Dict[str, Any] = field(default_factory=dict)
