"""
System-level tool registry assembly.

本模块负责在 **system 层** 按类别组装工具，并提供统一的
`build_tool_registry(profile: CoreProfile) -> VersionedToolRegistry` 工厂函数。

分类大致为：
- schedule: 日程 / 任务 / 时间解析 / 规划器
- file: 文件读写与修改
- web: （预留，当前由 MCP 工具负责）
- canvas: Canvas 课表与作业同步 / 查询
- shuiyuan: 水源社区相关工具
- automation: 自动化调度、摘要、通知等

注意：
- 实际可用工具仍以 `CoreProfile` 权限为准（allowed_tools / deny_tools /
  allow_dangerous_commands），本模块只负责默认装配。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from agent_core.config import Config, get_config
from agent_core.kernel_interface import CoreProfile
from agent_core.memory import ContentMemory, LongTermMemory
from agent_core.tools import (
    BaseTool,
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
    ShuiyuanSummarizeArchiveTool,
    AckNotificationTool,
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

    user_id = (memory_owner_id or os.getenv("SCHEDULE_USER_ID", "root")).strip() or "root"
    source = (memory_source or os.getenv("SCHEDULE_SOURCE", "cli")).strip() or "cli"

    from agent_core.agent.memory_paths import resolve_memory_owner_paths

    paths = resolve_memory_owner_paths(
        mem_cfg, user_id, config=config, source=source
    )

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
        tools.append(ShuiyuanSummarizeArchiveTool(config=config, batch_size=50))
    return tools


def _build_automation_tools(config: Config) -> List[BaseTool]:
    tools: List[BaseTool] = [
        SyncSourcesTool(),
        GetSyncStatusTool(),
        GetDigestTool(),
        ListNotificationsTool(),
        AckNotificationTool(),
        ConfigureAutomationPolicyTool(),
        GetAutomationActivityTool(),
        CreateScheduledJobTool(),
        NotifyOwnerTool(config=config),
    ]
    return tools


def build_tool_registry(
    profile: Optional[CoreProfile] = None,
    *,
    config: Optional[Config] = None,
    memory_owner_id: Optional[str] = None,
    memory_source: Optional[str] = None,
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
    tools.extend(
        _build_memory_tools(
            cfg,
            memory_owner_id=memory_owner_id,
            memory_source=memory_source or (profile.frontend_id if profile else None),
        )
    )
    tools.extend(_build_multimodal_tools(cfg))
    tools.extend(_build_canvas_tools(cfg))
    tools.extend(_build_shuiyuan_tools(cfg))
    tools.extend(_build_automation_tools(cfg))

    # 按 CoreProfile 过滤默认可见工具集合
    if profile is not None:
        for tool in tools:
            if profile.is_tool_allowed(tool.name):
                registry.register(tool)
    else:
        for tool in tools:
            registry.register(tool)

    return registry
