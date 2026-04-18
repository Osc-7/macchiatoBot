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
  background — 后台任务 Core（定时任务 / 心跳 / 监控，默认无记忆持久化，短 TTL）
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, fields
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent_core.config import Config, ToolsConfig


CHECKPOINT_CORE_PROFILE_FORMAT = 1


def _resolve_tool_template_config(
    *,
    tool_template: str,
    tools_config: Optional["ToolsConfig"] = None,
) -> tuple[str, Literal["pinned", "empty"], List[str]]:
    from agent_core.config import ToolsConfig

    cfg = tools_config or ToolsConfig()
    template_name = (tool_template or "default").strip() or "default"
    template = cfg.get_template(template_name)
    allowed = cfg.resolve_initial_tools(template_name)
    return template_name, template.exposure, allowed


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
        是否允许危险/破坏性命令；bash 工具始终注册，具体命令由 BashSecurity 再校验。

    visible_memory_scopes:
        允许 InternalLoader 加载的记忆层级。
        可选值：working / long_term / content / chat
        空列表表示不加载任何记忆（适合一次性无状态 Core）。

    max_context_tokens:
        触发 ContextOverflowAction 的 token 阈值。
        InternalLoader 在每轮完整 thought→tools→observations 后检查。

    session_expired_seconds:
        Kernel TTL 扫描依据：(now - last_active_ts) > 该值时触发 kill 流程。

    frontend_id / dialog_window_id:
        绑定的记忆库标识。memory_key = (frontend_id, dialog_window_id)。
        CorePool._load() 用这两个字段定位该 Core 应加载哪个记忆库。
    字段说明（除注释外的关键行为位）：

    - allowed_tools / deny_tools:
        工具白/黑名单，Kernel 在执行 ToolCallAction 时会再次校验。
    - allow_dangerous_commands:
        是否允许危险/破坏性命令（见字段说明）。
    - visible_memory_scopes:
        InternalLoader 允许加载的记忆层级（working / long_term / content / chat）。
        空列表表示不加载任何记忆（适合一次性无状态 Core）。
    - max_context_tokens:
        触发 ContextOverflowAction 的 token 阈值。
    - session_expired_seconds:
        TTL 超时时间，超过后由 KernelScheduler 触发 evict。
    - frontend_id / dialog_window_id:
        绑定的记忆库标识，memory_key = (frontend_id, dialog_window_id)。
        CorePool._load() 用这两个字段定位该 Core 应加载哪个记忆库。
    - memory_enabled:
        是否为该 Core 启用本地记忆库（data/memory/... 目录的创建与读写）。
        False 时，Core 仍可运行，但不会为该 Core 创建任何 owner 记忆目录。
    """

    mode: Literal["full", "sub", "background"] = "full"

    allowed_tools: Optional[List[str]] = None
    deny_tools: List[str] = field(default_factory=list)
    allow_dangerous_commands: bool = False

    visible_memory_scopes: List[str] = field(
        default_factory=lambda: ["working", "long_term", "content", "chat"]
    )

    # 是否为该 Core 启用本地记忆库（data/memory 下的 owner 目录）。
    # 关闭后，Agent 仍会运行，但不会创建 long_term/content/chat_history 等持久化目录，
    # 适合 background 模式（定时任务 / 心跳）等一次性或只读任务。
    memory_enabled: bool = True

    # bash 工作区：为 True 时该 Core 使用 command_tools.base_dir 作为初始 cwd，不启用按用户目录隔离与 cd 防护（全盘可访问，与 config.command_tools.workspace_admin_memory_owners 二选一配置方式）。
    bash_workspace_admin: bool = False

    max_context_tokens: Optional[int] = 80_000
    session_expired_seconds: int = 1_800

    # 子 Agent 专用：单次运行上限（None 表示不限制）
    max_iterations_override: Optional[int] = None
    max_total_tokens: Optional[int] = None

    frontend_id: str = ""
    dialog_window_id: str = ""
    tool_template: str = "default"
    tool_exposure_mode: Literal["pinned", "empty"] = "pinned"

    def is_tool_allowed(self, tool_name: str) -> bool:
        """判断指定工具名是否在该 Profile 的权限范围内。

        执行顺序：
        1. 如果 tool_name 在 deny_tools → False（黑名单优先）
        2. 核心工具（search_tools / call_tool / bash / request_permission / ask_user）始终允许
        3. 如果 allowed_tools 为 None → True（无白名单限制）
        4. 否则检查 allowed_tools 白名单
        """
        _CORE_TOOLS = {"search_tools", "call_tool", "bash", "request_permission", "ask_user"}

        if tool_name in self.deny_tools:
            return False
        if tool_name in _CORE_TOOLS:
            return True
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
        tool_template: str = "default",
        tools_config: Optional["ToolsConfig"] = None,
    ) -> "CoreProfile":
        """完整权限 Core（主对话场景）。"""
        resolved_template, exposure, _ = _resolve_tool_template_config(
            tool_template=tool_template,
            tools_config=tools_config,
        )
        return cls(
            mode="full",
            allowed_tools=None,
            allow_dangerous_commands=False,
            frontend_id=frontend_id,
            dialog_window_id=dialog_window_id,
            max_context_tokens=max_context_tokens,
            session_expired_seconds=session_expired_seconds,
            tool_template=resolved_template,
            tool_exposure_mode=exposure,
        )

    @classmethod
    def full_from_config(
        cls,
        config: "Config",
        *,
        frontend_id: str = "",
        dialog_window_id: str = "",
        tool_template: str = "default",
    ) -> "CoreProfile":
        """cli/feishu 主对话：按模板限制可用工具，危险命令按配置放行。"""
        agent_cfg = getattr(config, "agent", None)
        cmd_cfg = getattr(config, "command_tools", None)
        allow_dangerous = bool(
            cmd_cfg
            and getattr(cmd_cfg, "enabled", False)
            and getattr(cmd_cfg, "allow_run", False)
        )
        resolved_template, exposure, _ = _resolve_tool_template_config(
            tool_template=tool_template,
            tools_config=getattr(config, "tools", None),
        )
        return cls(
            mode="full",
            allowed_tools=None,
            allow_dangerous_commands=allow_dangerous,
            frontend_id=frontend_id,
            dialog_window_id=dialog_window_id,
            max_context_tokens=getattr(agent_cfg, "max_context_tokens", 300000),
            session_expired_seconds=getattr(agent_cfg, "session_expired_seconds", 3600),
            tool_template=resolved_template,
            tool_exposure_mode=exposure,
        )

    @classmethod
    def default_sub(
        cls,
        allowed_tools: Optional[List[str]] = None,
        *,
        deny_tools: Optional[List[str]] = None,
        frontend_id: str = "",
        dialog_window_id: str = "",
        max_iterations_override: Optional[int] = None,
        max_total_tokens: Optional[int] = None,
        max_context_tokens: Optional[int] = None,
        allow_dangerous_commands: bool = False,
        tool_template: str = "default",
        tools_config: Optional["ToolsConfig"] = None,
    ) -> "CoreProfile":
        """子 Agent / 工具 Agent（受限工具集；allow_dangerous_commands=True 时允许 bash 工具名，执行仍受 BashSecurity / 白名单约束）。

        max_context_tokens:
            profile 层上下文压缩阈值；None 表示不设该上限（仅由 WorkingMemory 的 max_tokens 约束）。
            历史上曾硬编码为 40_000，与 agent.subagent_max_tokens（累计用量）无关。
        """
        template_name = (tool_template or "default").strip() or "default"
        template_name, exposure, _ = _resolve_tool_template_config(
            tool_template=template_name,
            tools_config=tools_config,
        )
        deny_list = list(deny_tools) if deny_tools is not None else []
        return cls(
            mode="sub",
            allowed_tools=allowed_tools,
            deny_tools=deny_list,
            allow_dangerous_commands=allow_dangerous_commands,
            visible_memory_scopes=["working", "chat"],
            max_context_tokens=max_context_tokens,
            session_expired_seconds=300,
            frontend_id=frontend_id,
            dialog_window_id=dialog_window_id,
            max_iterations_override=max_iterations_override,
            max_total_tokens=max_total_tokens,
            tool_template=template_name,
            tool_exposure_mode=exposure,
        )

    @classmethod
    def default_background(
        cls,
        allowed_tools: Optional[List[str]] = None,
        *,
        frontend_id: str = "",
        dialog_window_id: str = "",
        tool_template: str = "cron",
        tools_config: Optional["ToolsConfig"] = None,
    ) -> "CoreProfile":
        """后台任务 Core（定时任务 / 心跳 / 监控；无记忆持久化，短 TTL）。"""
        template_name = (tool_template or "cron").strip() or "cron"
        template_name, exposure, _ = _resolve_tool_template_config(
            tool_template=template_name,
            tools_config=tools_config,
        )
        return cls(
            mode="background",
            allowed_tools=allowed_tools,
            allow_dangerous_commands=False,
            visible_memory_scopes=["long_term"],
            memory_enabled=False,
            max_context_tokens=40_000,
            session_expired_seconds=600,
            frontend_id=frontend_id,
            dialog_window_id=dialog_window_id,
            tool_template=template_name,
            tool_exposure_mode=exposure,
        )

    @classmethod
    def for_shuiyuan(
        cls,
        *,
        dialog_window_id: str = "",
        max_context_tokens: int = 200000,
        session_expired_seconds: int = 1800,
        tool_template: str = "shuiyuan",
        tools_config: Optional["ToolsConfig"] = None,
    ) -> "CoreProfile":
        """水源社区前端 Core：按普通前端走 full 权限，仍保留独立 frontend/user 记忆命名空间。"""
        resolved_template, exposure, _ = _resolve_tool_template_config(
            tool_template=tool_template,
            tools_config=tools_config,
        )
        return cls(
            mode="full",
            allowed_tools=None,
            allow_dangerous_commands=True,
            # 为每个水源用户名维护独立记忆与工作区命名空间
            frontend_id="shuiyuan",
            dialog_window_id=dialog_window_id,
            max_context_tokens=max_context_tokens,
            session_expired_seconds=session_expired_seconds,
            tool_template=resolved_template,
            tool_exposure_mode=exposure,
        )

    # 向后兼容别名
    default_cron = default_background
    default_heartbeat = default_background


def core_profile_to_checkpoint_dict(
    profile: Optional[CoreProfile],
) -> Optional[Dict[str, Any]]:
    """将 CoreProfile 序列化为可写入 checkpoint JSON 的 dict（None 表示未挂载 profile）。"""
    if profile is None:
        return None
    out = asdict(profile)
    out["_checkpoint_core_profile_format"] = CHECKPOINT_CORE_PROFILE_FORMAT
    return out


def core_profile_from_checkpoint_dict(
    data: Optional[Dict[str, Any]],
) -> Optional[CoreProfile]:
    """从 checkpoint dict 还原 CoreProfile；无法解析时返回 None（调用方走默认推断）。"""
    if not data or not isinstance(data, dict):
        return None
    payload = dict(data)
    payload.pop("_checkpoint_core_profile_format", None)
    field_names = {f.name for f in fields(CoreProfile)}
    kwargs = {k: v for k, v in payload.items() if k in field_names}
    try:
        return CoreProfile(**kwargs)
    except TypeError as exc:
        logger.warning("CoreProfile checkpoint deserialize failed: %s", exc)
        return None
