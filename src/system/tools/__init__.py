"""
System-level tool registry assembly.

本模块负责在 **system 层** 按类别组装工具，并提供统一的
`build_tool_registry(profile: CoreProfile) -> VersionedToolRegistry` 与 `get_default_tools(config)`。

分类大致为：
- schedule: 日程 / 任务 / 时间解析 / 规划器
- file: 文件读写与修改
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
    from system.kernel.core_pool import CorePool

from agent_core.config import Config, get_config
from agent_core.kernel_interface import CoreProfile
from agent_core.orchestrator import ToolWorkingSetManager
from agent_core.memory import ContentMemory, LongTermMemory
from agent_core.tools import (
    BaseTool,
    CallToolTool,
    SearchToolsTool,
    VersionedToolRegistry,
)
from agent_core.memory.chat_history_db import ChatHistoryDB

from .parse_time import ParseTimeTool
from .planner_tools import GetFreeSlotsTool, PlanTasksTool
from .storage_tools import (
    AddEventTool,
    AddTaskTool,
    GetEventsTool,
    GetTasksTool,
    UpdateEventTool,
    UpdateTaskTool,
    DeleteScheduleDataTool,
)
from .file_tools import ReadFileTool, WriteFileTool, ModifyFileTool
from .memory_tools import (
    MemorySearchLongTermTool,
    MemorySearchContentTool,
    MemoryStoreTool,
    MemoryIngestTool,
)
from .chat_history_tools import (
    ChatContextTool,
    ChatScrollTool,
    ChatSearchTool,
)
from .media_tools import AttachMediaTool, AttachImageToReplyTool
from .canvas_tools import (
    SyncCanvasTool,
    FetchCanvasOverviewTool,
    FetchCanvasCourseContentTool,
)
from .automation_tools import (
    SyncSourcesTool,
    GetSyncStatusTool,
    GetDigestTool,
    ListNotificationsTool,
    AckNotificationTool,
    ConfigureAutomationPolicyTool,
    GetAutomationActivityTool,
    CreateScheduledJobTool,
    NotifyOwnerTool,
)
from .sjtu_jw_tools import FetchSjtuUndergradScheduleTool
from .shuiyuan_tools import (
    ShuiyuanBrowseTopicTool,
    ShuiyuanGetCategoriesTool,
    ShuiyuanGetCategoryTopicsTool,
    ShuiyuanGetLatestTool,
    ShuiyuanGetTopicTool,
    ShuiyuanGetTopTool,
    ShuiyuanPostReplyTool,
    ShuiyuanRetortTool,
    ShuiyuanSearchTool,
)
from .subagent_tools import (
    CancelSubagentTool,
    CreateParallelSubagentsTool,
    CreateSubagentTool,
    GetSubagentStatusTool,
    ListAgentsTool,
    ReapSubagentTool,
    ReplyToMessageTool,
    SendMessageToAgentTool,
)
from .factory import get_default_tools

__all__ = [
    "VersionedToolRegistry",
    "build_tool_registry",
    "get_default_tools",
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
    # BashTool 由 AgentCore.__aenter__ 自注册（绑定 Core 自身的 BashRuntime），
    # 不再通过 build_tool_registry 外部装配。保留空函数签名以维持调用链。
    return []


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
        tools.append(AttachImageToReplyTool(config=config))
    return tools


def _build_canvas_tools(config: Config) -> List[BaseTool]:
    tools: List[BaseTool] = [
        SyncCanvasTool(config=config),
        FetchCanvasOverviewTool(config=config),
        FetchCanvasCourseContentTool(config=config),
    ]
    return tools


def _build_shuiyuan_tools(config: Config) -> List[BaseTool]:
    """与 factory.get_default_tools 中水源块保持一致，供 build_tool_registry / CorePool 使用。"""
    tools: List[BaseTool] = []
    shuiyuan_cfg = getattr(config, "shuiyuan", None)
    if shuiyuan_cfg and getattr(shuiyuan_cfg, "enabled", False):
        tools.append(ShuiyuanSearchTool(config=config))
        tools.append(ShuiyuanGetTopicTool(config=config))
        tools.append(ShuiyuanRetortTool(config=config))
        tools.append(ShuiyuanPostReplyTool(config=config))
        tools.append(ShuiyuanGetLatestTool(config=config))
        tools.append(ShuiyuanGetTopTool(config=config))
        tools.append(ShuiyuanGetCategoriesTool(config=config))
        tools.append(ShuiyuanGetCategoryTopicsTool(config=config))
        tools.append(ShuiyuanBrowseTopicTool(config=config))
    return tools


def _build_skill_tools(config: Config) -> List[BaseTool]:
    tools: List[BaseTool] = []
    skills_cfg = getattr(config, "skills", None)
    if skills_cfg is not None and (
        getattr(skills_cfg, "enabled", None) or getattr(skills_cfg, "cli_dir", None)
    ):
        try:
            from .load_skill_tool import LoadSkillTool

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
    default_memory_owner: Optional[str] = None
    default_core_mode: Optional[str] = None
    default_tool_template: Optional[str] = None
    if profile is not None:
        default_core_mode = getattr(profile, "mode", None) or "background"
        default_tool_template = getattr(profile, "tool_template", None) or None
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
            default_tool_template=default_tool_template,
        ),
        NotifyOwnerTool(config=config),
    ]
    return tools


def _build_subagent_tools(
    profile: Optional[CoreProfile] = None,
    *,
    core_pool: Optional["CorePool"] = None,
) -> List[BaseTool]:
    tools: List[BaseTool] = []
    if core_pool is None:
        return tools

    mode = getattr(profile, "mode", "full") if profile else "full"

    if mode == "sub":
        tools.append(_LazySchedulerSendMessageTool(core_pool))
        tools.append(_LazySchedulerReplyToMessageTool(core_pool))
        tools.append(ListAgentsTool(core_pool=core_pool))
    else:
        tools.append(
            CreateSubagentTool(
                core_pool=core_pool,
                scheduler=_SchedulerProxy(core_pool),
            )
        )
        tools.append(
            CreateParallelSubagentsTool(
                core_pool=core_pool,
                scheduler=_SchedulerProxy(core_pool),
            )
        )
        tools.append(_LazySchedulerSendMessageTool(core_pool))
        tools.append(_LazySchedulerReplyToMessageTool(core_pool))
        tools.append(GetSubagentStatusTool(core_pool=core_pool))
        tools.append(ReapSubagentTool(core_pool=core_pool))
        tools.append(CancelSubagentTool(core_pool=core_pool))
        tools.append(ListAgentsTool(core_pool=core_pool))

    return tools


class _SchedulerProxy:
    def __init__(self, core_pool: "CorePool") -> None:
        self._core_pool = core_pool

    def inject_turn(self, request) -> None:  # type: ignore[override]
        s = self._core_pool._scheduler
        if s is None:
            raise RuntimeError("KernelScheduler not yet bound to CorePool")
        s.inject_turn(request)

    async def submit(self, request):  # type: ignore[override]
        s = self._core_pool._scheduler
        if s is None:
            raise RuntimeError("KernelScheduler not yet bound to CorePool")
        return await s.submit(request)

    async def wait_result(self, request_id: str, timeout_seconds: float | None = None):  # type: ignore[override]
        s = self._core_pool._scheduler
        if s is None:
            raise RuntimeError("KernelScheduler not yet bound to CorePool")
        return await s.wait_result(request_id, timeout_seconds=timeout_seconds)


class _LazySchedulerSendMessageTool(SendMessageToAgentTool):
    def __init__(self, core_pool: "CorePool") -> None:
        self._core_pool = core_pool

    @property
    def _scheduler(self):  # type: ignore[override]
        s = self._core_pool._scheduler
        if s is None:
            raise RuntimeError("KernelScheduler not yet bound to CorePool")
        return s

    def _check_sender_cancelled(self, sender_session_id: str):
        if not sender_session_id.startswith("sub:"):
            return None
        entry = self._core_pool.get_sub_info(sender_session_id)
        if entry is not None and entry.sub_status == "cancelled":
            from agent_core.tools.base import ToolResult
            return ToolResult(
                success=False,
                message="子 Agent 已被取消，无法发送消息",
                error="SUBAGENT_CANCELLED",
            )
        return None


class _LazySchedulerReplyToMessageTool(ReplyToMessageTool):
    def __init__(self, core_pool: "CorePool") -> None:
        self._core_pool = core_pool

    @property
    def _scheduler(self):  # type: ignore[override]
        s = self._core_pool._scheduler
        if s is None:
            raise RuntimeError("KernelScheduler not yet bound to CorePool")
        return s


def build_tool_registry(
    profile: Optional[CoreProfile] = None,
    *,
    config: Optional[Config] = None,
    memory_owner_id: Optional[str] = None,
    memory_source: Optional[str] = None,
    core_pool: Optional["CorePool"] = None,
    filter_by_profile: bool = True,
) -> VersionedToolRegistry:
    cfg = config or get_config()
    registry = VersionedToolRegistry()

    tools: List[BaseTool] = []
    tools.extend(_build_schedule_tools(cfg))
    tools.extend(_build_file_tools(cfg))
    tools.extend(_build_command_tools(cfg))

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
            core_pool=core_pool,
        )
    )

    if profile is not None and filter_by_profile:
        for tool in tools:
            if profile.is_tool_allowed(tool.name):
                registry.register(tool)
    else:
        for tool in tools:
            registry.register(tool)

    agent_cfg = getattr(cfg, "agent", None)
    tools_cfg = getattr(cfg, "tools", None)
    pinned = list(
        getattr(tools_cfg, "core_tools", None)
        or ["search_tools", "call_tool", "bash", "request_permission"]
    )
    for core in ["search_tools", "call_tool", "bash", "request_permission"]:
        if core not in pinned:
            pinned.append(core)
    if profile is not None:
        template_name = getattr(profile, "tool_template", "default")
        template = tools_cfg.get_template(template_name) if tools_cfg else None
        exposure = getattr(profile, "tool_exposure_mode", None) or (
            template.exposure if template is not None else "pinned"
        )
        if exposure == "pinned":
            pinned.extend(list(getattr(tools_cfg, "pinned_tools", None) or []))
        if template is not None:
            pinned.extend(list(template.extra or []))
    pinned = [name for idx, name in enumerate(pinned) if name and name not in pinned[:idx]]
    working_set_size = int(getattr(agent_cfg, "working_set_size", 6) or 6)
    working_set = ToolWorkingSetManager(
        pinned_tools=pinned, working_set_size=working_set_size
    )
    # call_tool 按名查的是本 registry；须与 AgentCore._tool_registry 中的 meta 工具一致，
    # 否则 LLM 通过 call_tool 调用 request_permission 会报 TOOL_NOT_FOUND（直接函数名调用仍走 session registry）。
    if not registry.has("request_permission"):
        from agent_core.tools.request_permission_tool import RequestPermissionTool

        if profile is None or profile.is_tool_allowed("request_permission"):
            registry.register(RequestPermissionTool())
    if not registry.has("search_tools"):
        registry.register(
            SearchToolsTool(
                registry=registry,
                working_set=working_set,
                profile_getter=(lambda: profile) if profile is not None else None,
            )
        )
    if not registry.has("call_tool"):
        registry.register(CallToolTool(registry=registry))

    return registry
