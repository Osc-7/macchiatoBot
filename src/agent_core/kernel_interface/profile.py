"""
CoreProfile — Core 实例的权限与配置描述符。

类比操作系统的进程权限集合（capability set）：
- Kernel 在创建 Core 时将 CoreProfile 写入 CoreEntry
- InternalLoader 用 profile 过滤暴露给 LLM 的工具列表（用户态防御）
- AgentKernel 在执行 ToolCallAction 时校验 profile（内核态强制）
- CoreProfile.session_expired_seconds 是 Kernel TTL 扫描的依据

mode 枚举语义：
  full       — 完整权限 Agent（主对话，默认）
  sub        — 子 Agent / 工具 Agent（受限工具集，通常无危险命令）
  cron       — 定时任务触发的一次性 Core（按任务配置赋权）
  heartbeat  — 心跳/监控 Core（只读权限，通常只能 notify/read）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional


@dataclass
class CoreProfile:
    """Core 实例的权限与行为配置。

    allowed_tools:
        可调用的工具名称白名单。None 表示继承全量工具（由 Kernel 全局注册表决定）。
        与 deny_tools 同时存在时，先过白名单再减黑名单。

    deny_tools:
        强制禁用的工具名称列表，优先级高于 allowed_tools。
        Kernel 执行 ToolCallAction 时会二次校验，即使 LLM 发出了请求也会拒绝。

    allow_dangerous_commands:
        是否允许执行危险 shell 命令（RunCommandTool）。
        False 时 RunCommandTool 自动加入 deny_tools（内核态强制）。

    visible_memory_scopes:
        允许 InternalLoader 加载的记忆层级。
        可选值：working / short_term / long_term / content / chat
        空列表表示不加载任何记忆（适合一次性无状态 Core）。

    max_context_tokens:
        触发 ContextOverflowAction 的 token 阈值。
        InternalLoader 在每轮完整 thought→tools→observations 后检查。

    session_expired_seconds:
        Kernel TTL 扫描依据：(now - last_active_ts) > 该值时触发 kill 流程。

    frontend_id / dialog_window_id:
        绑定的记忆库标识。memory_key = (frontend_id, dialog_window_id)。
        CorePool._load() 用这两个字段定位该 Core 应加载哪个记忆库。
    """

    mode: Literal["full", "sub", "cron", "heartbeat"] = "full"

    allowed_tools: Optional[List[str]] = None
    deny_tools: List[str] = field(default_factory=list)
    allow_dangerous_commands: bool = False

    visible_memory_scopes: List[str] = field(
        default_factory=lambda: ["working", "short_term", "long_term", "content", "chat"]
    )

    max_context_tokens: int = 80_000
    session_expired_seconds: int = 1_800

    frontend_id: str = ""
    dialog_window_id: str = ""

    def is_tool_allowed(self, tool_name: str) -> bool:
        """判断指定工具名是否在该 Profile 的权限范围内。

        执行顺序：
        1. 如果 tool_name 在 deny_tools → False（黑名单优先）
        2. 如果 allow_dangerous_commands=False 且 tool 是危险命令工具 → False
        3. 如果 allowed_tools 为 None → True（无白名单限制）
        4. 否则检查 allowed_tools 白名单
        """
        _DANGEROUS_TOOLS = {"run_command"}

        if tool_name in self.deny_tools:
            return False
        if not self.allow_dangerous_commands and tool_name in _DANGEROUS_TOOLS:
            return False
        if self.allowed_tools is None:
            return True
        return tool_name in self.allowed_tools

    def filter_tools(self, tool_names: List[str]) -> List[str]:
        """从给定工具名列表中过滤出该 Profile 允许的子集，保持原顺序。"""
        return [name for name in tool_names if self.is_tool_allowed(name)]

    @classmethod
    def default_full(
        cls,
        *,
        frontend_id: str = "",
        dialog_window_id: str = "",
        max_context_tokens: int = 80_000,
        session_expired_seconds: int = 1_800,
    ) -> "CoreProfile":
        """完整权限 Core（主对话场景）。"""
        return cls(
            mode="full",
            allow_dangerous_commands=False,
            frontend_id=frontend_id,
            dialog_window_id=dialog_window_id,
            max_context_tokens=max_context_tokens,
            session_expired_seconds=session_expired_seconds,
        )

    @classmethod
    def default_sub(
        cls,
        allowed_tools: Optional[List[str]] = None,
        *,
        frontend_id: str = "",
        dialog_window_id: str = "",
    ) -> "CoreProfile":
        """子 Agent / 工具 Agent（受限工具集，不允许危险命令，不加载长期记忆）。"""
        return cls(
            mode="sub",
            allowed_tools=allowed_tools,
            allow_dangerous_commands=False,
            visible_memory_scopes=["working", "chat"],
            max_context_tokens=40_000,
            session_expired_seconds=300,
            frontend_id=frontend_id,
            dialog_window_id=dialog_window_id,
        )

    @classmethod
    def default_cron(
        cls,
        allowed_tools: Optional[List[str]] = None,
        *,
        frontend_id: str = "",
        dialog_window_id: str = "",
    ) -> "CoreProfile":
        """定时任务 Core（一次性，无聊天记忆，按任务配置赋工具权限）。"""
        return cls(
            mode="cron",
            allowed_tools=allowed_tools,
            allow_dangerous_commands=False,
            visible_memory_scopes=["long_term"],
            max_context_tokens=40_000,
            session_expired_seconds=600,
            frontend_id=frontend_id,
            dialog_window_id=dialog_window_id,
        )

    @classmethod
    def default_heartbeat(
        cls,
        *,
        frontend_id: str = "",
        dialog_window_id: str = "",
    ) -> "CoreProfile":
        """心跳/监控 Core（只读，只能通知，极短 TTL）。"""
        return cls(
            mode="heartbeat",
            allowed_tools=["notify_owner", "get_events", "get_tasks", "get_sync_status"],
            allow_dangerous_commands=False,
            visible_memory_scopes=[],
            max_context_tokens=20_000,
            session_expired_seconds=120,
            frontend_id=frontend_id,
            dialog_window_id=dialog_window_id,
        )
