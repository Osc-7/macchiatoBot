"""
System-level tool registry assembly.

本模块负责在 **system 层** 按类别组装工具，并提供统一的
`build_tool_registry(profile: CoreProfile) -> VersionedToolRegistry` 工厂函数。

分类大致为：
- schedule: 日程 / 任务 / 时间解析 / 规划器
- file: 文件读写与修改
- web: （预留，当前由 MCP 工具负责）
- memory: 长期记忆、内容记忆、chat_history 检索
- canvas: Canvas 课表与作业同步 / 查询
- shuiyuan: 水源社区相关工具
- automation: 自动化调度、摘要、通知等

注意：
- 实际可用工具仍以 `CoreProfile` 权限为准（allowed_tools / deny_tools /
  allow_dangerous_commands），本模块只负责默认装配。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from system.kernel.subagent_registry import SubagentRegistry
    from system.kernel.core_pool import CorePool

from agent_core.config import Config, get_config
from agent_core.kernel_interface import CoreProfile
from agent_core.orchestrator import ToolWorkingSetManager
from agent_core.memory import ContentMemory, LongTermMemory
from agent_core.tools import (
    BaseTool,
    CallToolTool,
    CancelSubagentTool,
    CreateParallelSubagentsTool,
    CreateSubagentTool,
    GetSubagentStatusTool,
    ReplyToMessageTool,
    SearchToolsTool,
    SendMessageToAgentTool,
    VersionedToolRegistry,
    AddEventTool,
    AddTaskTool,
    AttachImageToReplyTool,
    AttachMediaTool,
    ConfigureAutomationPolicyTool,
    CreateScheduledJobTool,
    DeleteScheduleDataTool,
    GetAutomationActivityTool,
    GetDigestTool,
    GetEventsTool,
    GetFreeSlotsTool,
    GetSyncStatusTool,
    GetTasksTool,
    ListNotificationsTool,
    MemoryIngestTool,
    MemorySearchContentTool,
    MemorySearchLongTermTool,
    MemoryStoreTool,
    ModifyFileTool,
    NotifyOwnerTool,
    ParseTimeTool,
    PlanTasksTool,
    ReadFileTool,
    RunCommandTool,
    SyncCanvasTool,
    SyncSourcesTool,
    UpdateEventTool,
    UpdateTaskTool,
    WriteFileTool,
    FetchCanvasCourseContentTool,
    FetchCanvasOverviewTool,
    FetchSjtuUndergradScheduleTool,
    ShuiyuanGetTopicTool,
    ShuiyuanPostReplyTool,
    ShuiyuanRetortTool,
    ShuiyuanSearchTool,
    AckNotificationTool,
)
from agent_core.memory.chat_history_db import ChatHistoryDB
from agent_core.tools.chat_history_tools import (
    ChatContextTool,
    ChatScrollTool,
    ChatSearchTool,
)

__all__ = [
    "VersionedToolRegistry",
    "build_tool_registry",
]


def _build_schedule_tools(config: Config) -> List[BaseTool]:
    tools: List[BaseTool] = [
        ParseTimeTool(),
        AddEventTool(),
        AddTaskTool(),
        GetEventsTool(),
        GetTasksTool(),
        UpdateEventTool(),
        UpdateTaskTool(),
        DeleteScheduleDataTool(),
        GetFreeSlotsTool(),
        PlanTasksTool(planning_config=getattr(config, "planning", None)),
    ]
    # 交大教学信息服务网课表同步工具（基于 Cookie，只读）
    try:
        sjtu_cfg = getattr(config, "sjtu_jw", None)
        if sjtu_cfg is not None:
            tools.append(
                FetchSjtuUndergradScheduleTool(
                    cookies_path=sjtu_cfg.cookies_path,
                    config=sjtu_cfg,
                )
            )
        else:
            tools.append(FetchSjtuUndergradScheduleTool())
    except Exception:
        # 配置不完整时退化为无参数版本，避免阻塞整个注册表构建
        tools.append(FetchSjtuUndergradScheduleTool())
    return tools


def _build_file_tools(config: Config) -> List[BaseTool]:
    tools: List[BaseTool] = []
    file_cfg = getattr(config, "file_tools", None)
    if file_cfg and getattr(file_cfg, "enabled", False):
        tools.append(ReadFileTool(config=config))
        tools.append(WriteFileTool(config=config))
        tools.append(ModifyFileTool(config=config))
    return tools


def _build_command_tools(config: Config) -> List[BaseTool]:
    tools: List[BaseTool] = []
    cmd_cfg = getattr(config, "command_tools", None)
    if cmd_cfg and getattr(cmd_cfg, "enabled", False):
        tools.append(RunCommandTool(config=config))
    return tools


def _build_memory_tools(
    config: Config,
    *,
    memory_owner_id: Optional[str] = None,
    memory_source: Optional[str] = None,
) -> List[BaseTool]:
    tools: List[BaseTool] = []
    mem_cfg = getattr(config, "memory", None)
    if not mem_cfg or not getattr(mem_cfg, "enabled", False):
        return tools

    user_id = (
        memory_owner_id or os.getenv("SCHEDULE_USER_ID", "root")
    ).strip() or "root"
    source = (memory_source or os.getenv("SCHEDULE_SOURCE", "cli")).strip() or "cli"

    from agent_core.agent.memory_paths import resolve_memory_owner_paths

    paths = resolve_memory_owner_paths(mem_cfg, user_id, config=config, source=source)

    long_term = LongTermMemory(
        storage_dir=paths["long_term_dir"],
        memory_md_path=paths["memory_md_path"],
        qmd_enabled=mem_cfg.qmd_enabled,
        qmd_command=mem_cfg.qmd_command,
    )
    content = ContentMemory(
        content_dir=paths["content_dir"],
        qmd_enabled=mem_cfg.qmd_enabled,
        qmd_command=mem_cfg.qmd_command,
    )
    top_n = mem_cfg.recall_top_n
    tools.append(MemorySearchLongTermTool(long_term, top_n))
    tools.append(MemorySearchContentTool(content, top_n))
    tools.append(MemoryStoreTool(content))
    tools.append(MemoryIngestTool(content))
    return tools


def _build_chat_history_tools(
    config: Config,
    *,
    memory_owner_id: Optional[str] = None,
    memory_source: Optional[str] = None,
) -> List[BaseTool]:
    """对话历史检索工具：chat_search、chat_context、chat_scroll。"""
    tools: List[BaseTool] = []
    mem_cfg = getattr(config, "memory", None)
    if not mem_cfg or not getattr(mem_cfg, "enabled", False):
        return tools

    user_id = (
        memory_owner_id or os.getenv("SCHEDULE_USER_ID", "root")
    ).strip() or "root"
    source = (memory_source or os.getenv("SCHEDULE_SOURCE", "cli")).strip() or "cli"

    from agent_core.agent.memory_paths import resolve_memory_owner_paths

    paths = resolve_memory_owner_paths(mem_cfg, user_id, config=config, source=source)
    chat_db = ChatHistoryDB(
        paths["chat_history_db_path"],
        default_source=None,
    )
    tools.append(ChatSearchTool(chat_db))
    tools.append(ChatContextTool(chat_db))
    tools.append(ChatScrollTool(chat_db))
    return tools


def _build_multimodal_tools(config: Config) -> List[BaseTool]:
    tools: List[BaseTool] = []
    mm_cfg = getattr(config, "multimodal", None)
    if mm_cfg and getattr(mm_cfg, "enabled", False):
        tools.append(AttachMediaTool())
        tools.append(AttachImageToReplyTool())
    return tools


def _build_canvas_tools(config: Config) -> List[BaseTool]:
    # Canvas 工具始终注册（便于 search_tools 发现），具体可用性由工具内部校验
    tools: List[BaseTool] = [
        SyncCanvasTool(config=config),
        FetchCanvasOverviewTool(config=config),
        FetchCanvasCourseContentTool(config=config),
    ]
    return tools


def _build_shuiyuan_tools(config: Config) -> List[BaseTool]:
    tools: List[BaseTool] = []
    shuiyuan_cfg = getattr(config, "shuiyuan", None)
    if shuiyuan_cfg and getattr(shuiyuan_cfg, "enabled", False):
        tools.append(ShuiyuanSearchTool(config=config))
        tools.append(ShuiyuanGetTopicTool(config=config))
        tools.append(ShuiyuanRetortTool(config=config))
        tools.append(ShuiyuanPostReplyTool(config=config))
    return tools


def _build_skill_tools(config: Config) -> List[BaseTool]:
    """LoadSkillTool：当配置了 skills.enabled 或 skills.cli_dir 时注册。"""
    tools: List[BaseTool] = []
    skills_cfg = getattr(config, "skills", None)
    if skills_cfg is not None and (
        getattr(skills_cfg, "enabled", None) or getattr(skills_cfg, "cli_dir", None)
    ):
        try:
            from agent_core.tools.load_skill_tool import LoadSkillTool

            tools.append(LoadSkillTool(config=config))
        except Exception:
            pass
    return tools


def _build_automation_tools(
    config: Config,
    *,
    profile: Optional[CoreProfile] = None,
    memory_owner_id: Optional[str] = None,
    memory_source: Optional[str] = None,
) -> List[BaseTool]:
    # 为 CreateScheduledJobTool 注入当前 Core 的权限默认值
    default_memory_owner: Optional[str] = None
    default_core_mode: Optional[str] = None
    if profile is not None:
        default_core_mode = getattr(profile, "mode", None) or "background"
        if getattr(profile, "memory_enabled", False):
            src = getattr(profile, "frontend_id", None) or memory_source or ""
            uid = (
                getattr(profile, "dialog_window_id", None)
                or memory_owner_id
                or "default"
            )
            if src and uid:
                default_memory_owner = f"{src}:{uid}"

    tools: List[BaseTool] = [
        SyncSourcesTool(),
        GetSyncStatusTool(),
        GetDigestTool(),
        ListNotificationsTool(),
        AckNotificationTool(),
        ConfigureAutomationPolicyTool(),
        GetAutomationActivityTool(),
        CreateScheduledJobTool(
            default_memory_owner=default_memory_owner,
            default_core_mode=default_core_mode,
        ),
        NotifyOwnerTool(config=config),
    ]
    return tools


def _build_subagent_tools(
    profile: Optional[CoreProfile] = None,
    *,
    subagent_registry: Optional["SubagentRegistry"] = None,
    core_pool: Optional["CorePool"] = None,
) -> List[BaseTool]:
    """
    装配 multi-agent 通信工具。

    mode="sub" 时只注册通信工具（send_message_to_agent + reply_to_message），
    防止子 Agent 无限孵化。
    mode="full" / 其他时注册完整 5 个工具（需要 registry + core_pool + scheduler）。
    """
    tools: List[BaseTool] = []
    if subagent_registry is None:
        return tools

    # scheduler 通过 registry 持有（registry.set_scheduler 后绑定），
    # 工具初始化时直接引用 registry._scheduler（懒取值）。
    # 为保持解耦，scheduler 在工具 execute() 时通过 registry 获取。
    # 但 SendMessageToAgentTool / ReplyToMessageTool 需要直接持有 scheduler 引用，
    # 在 registry 后绑定后从 registry._scheduler 取。
    mode = getattr(profile, "mode", "full") if profile else "full"

    if mode == "sub":
        # Sub agent 只能通信，不能孵化
        tools.append(_LazySchedulerSendMessageTool(subagent_registry))
        tools.append(_LazySchedulerReplyToMessageTool(subagent_registry))
    else:
        # Full / background agent：完整 5 个工具
        if core_pool is not None:
            tools.append(
                CreateSubagentTool(
                    registry=subagent_registry,
                    core_pool=core_pool,
                    scheduler=_SchedulerProxy(subagent_registry),
                )
            )
            tools.append(
                CreateParallelSubagentsTool(
                    registry=subagent_registry,
                    core_pool=core_pool,
                    scheduler=_SchedulerProxy(subagent_registry),
                )
            )
        tools.append(_LazySchedulerSendMessageTool(subagent_registry))
        tools.append(_LazySchedulerReplyToMessageTool(subagent_registry))
        tools.append(GetSubagentStatusTool(registry=subagent_registry))
        tools.append(CancelSubagentTool(registry=subagent_registry))

    return tools


class _SchedulerProxy:
    """
    懒加载 scheduler 代理。

    SubagentRegistry.set_scheduler() 在 daemon 初始化末尾调用（后绑定），
    工具在此之前已被装配。使用代理在 execute() 时才真正访问 scheduler，
    保证时序正确。
    """

    def __init__(self, registry: "SubagentRegistry") -> None:
        self._registry = registry

    def inject_turn(self, request) -> None:  # type: ignore[override]
        s = self._registry._scheduler
        if s is None:
            raise RuntimeError("KernelScheduler not yet bound to SubagentRegistry")
        s.inject_turn(request)

    async def submit(self, request):  # type: ignore[override]
        s = self._registry._scheduler
        if s is None:
            raise RuntimeError("KernelScheduler not yet bound to SubagentRegistry")
        return await s.submit(request)

    async def wait_result(self, request_id: str, timeout_seconds: float | None = None):  # type: ignore[override]
        s = self._registry._scheduler
        if s is None:
            raise RuntimeError("KernelScheduler not yet bound to SubagentRegistry")
        return await s.wait_result(request_id, timeout_seconds=timeout_seconds)


class _LazySchedulerSendMessageTool(SendMessageToAgentTool):
    """send_message_to_agent：通过 registry 懒加载 scheduler。"""

    def __init__(self, registry: "SubagentRegistry") -> None:
        self._registry = registry
        # 不调用父类 __init__（scheduler 懒加载）

    @property
    def _scheduler(self):  # type: ignore[override]
        s = self._registry._scheduler
        if s is None:
            raise RuntimeError("KernelScheduler not yet bound to SubagentRegistry")
        return s

    def _check_sender_cancelled(self, sender_session_id: str):
        """拒绝已取消的 subagent 发送消息（纵深防御）。"""
        if not sender_session_id.startswith("sub:"):
            return None
        subagent_id = sender_session_id[4:]
        info = self._registry.get(subagent_id)
        if info is not None and info.status == "cancelled":
            from agent_core.tools.base import ToolResult
            return ToolResult(
                success=False,
                message="子 Agent 已被取消，无法发送消息",
                error="SUBAGENT_CANCELLED",
            )
        return None


class _LazySchedulerReplyToMessageTool(ReplyToMessageTool):
    """reply_to_message：通过 registry 懒加载 scheduler。"""

    def __init__(self, registry: "SubagentRegistry") -> None:
        self._registry = registry

    @property
    def _scheduler(self):  # type: ignore[override]
        s = self._registry._scheduler
        if s is None:
            raise RuntimeError("KernelScheduler not yet bound to SubagentRegistry")
        return s


def build_tool_registry(
    profile: Optional[CoreProfile] = None,
    *,
    config: Optional[Config] = None,
    memory_owner_id: Optional[str] = None,
    memory_source: Optional[str] = None,
    subagent_registry: Optional["SubagentRegistry"] = None,
    core_pool: Optional["CorePool"] = None,
) -> VersionedToolRegistry:
    """
    构建带版本号的工具注册表。

    Args:
        profile: CoreProfile（控制默认注册的工具子集）。
        config: 可选显式 Config；缺省时使用全局 get_config()。

    Returns:
        VersionedToolRegistry 实例。
    """
    cfg = config or get_config()
    registry = VersionedToolRegistry()

    # 分类别装配工具
    tools: List[BaseTool] = []
    tools.extend(_build_schedule_tools(cfg))
    tools.extend(_build_file_tools(cfg))
    tools.extend(_build_command_tools(cfg))

    # 记忆与对话历史工具：
    # - CoreProfile.memory_enabled=False 时不注入，避免为每个一次性 Core 创建独立 data/memory/{source}/{user}/ 目录
    # - memory_enabled=True 时启用（包括部分 background Core 显式打开记忆的场景）
    memory_enabled = getattr(profile, "memory_enabled", True)
    if memory_enabled:
        tools.extend(
            _build_memory_tools(
                cfg,
                memory_owner_id=memory_owner_id,
                memory_source=memory_source
                or (profile.frontend_id if profile else None),
            )
        )
        tools.extend(
            _build_chat_history_tools(
                cfg,
                memory_owner_id=memory_owner_id,
                memory_source=memory_source
                or (profile.frontend_id if profile else None),
            )
        )
    tools.extend(_build_multimodal_tools(cfg))
    tools.extend(_build_canvas_tools(cfg))
    tools.extend(_build_shuiyuan_tools(cfg))
    tools.extend(_build_skill_tools(cfg))
    tools.extend(
        _build_automation_tools(
            cfg,
            profile=profile,
            memory_owner_id=memory_owner_id,
            memory_source=memory_source or (profile.frontend_id if profile else None),
        )
    )
    tools.extend(
        _build_subagent_tools(
            profile=profile,
            subagent_registry=subagent_registry,
            core_pool=core_pool,
        )
    )

    # 按 CoreProfile 过滤默认可见工具集合
    if profile is not None:
        for tool in tools:
            if profile.is_tool_allowed(tool.name):
                registry.register(tool)
    else:
        for tool in tools:
            registry.register(tool)

    # kernel 模式核心工具：search_tools、call_tool（Kernel 调度时需在此注册）
    agent_cfg = getattr(cfg, "agent", None)
    pinned = list(
        getattr(agent_cfg, "pinned_tools", None) or ["search_tools", "call_tool"]
    )
    for core in ["search_tools", "call_tool"]:
        if core not in pinned:
            pinned.append(core)
    working_set_size = int(getattr(agent_cfg, "working_set_size", 6) or 6)
    working_set = ToolWorkingSetManager(
        pinned_tools=pinned, working_set_size=working_set_size
    )
    if not registry.has("search_tools"):
        registry.register(SearchToolsTool(registry=registry, working_set=working_set))
    if not registry.has("call_tool"):
        registry.register(CallToolTool(registry=registry))

    return registry
