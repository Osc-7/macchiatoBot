"""
工具工厂 — 根据配置构建 Agent 可用的默认工具列表。

所有前端 / daemon 共享此工厂，避免各入口重复实例化逻辑。
"""

from __future__ import annotations

import os
from typing import List, Optional

from agent_core.config import Config
from agent_core.tools.base import BaseTool
from agent_core.tools.parse_time import ParseTimeTool
from agent_core.tools.planner_tools import GetFreeSlotsTool, PlanTasksTool
from agent_core.tools.storage_tools import (
    AddEventTool,
    AddTaskTool,
    GetEventsTool,
    GetTasksTool,
    UpdateEventTool,
    UpdateTaskTool,
    DeleteScheduleDataTool,
)
from agent_core.tools.file_tools import ReadFileTool, WriteFileTool, ModifyFileTool
from agent_core.tools.command_tools import RunCommandTool
from agent_core.tools.load_skill_tool import LoadSkillTool
from agent_core.tools.memory_tools import (
    MemorySearchLongTermTool,
    MemorySearchContentTool,
    MemoryStoreTool,
    MemoryIngestTool,
)
from agent_core.tools.media_tools import AttachMediaTool, AttachImageToReplyTool
from agent_core.tools.canvas_tools import (
    SyncCanvasTool,
    FetchCanvasOverviewTool,
    FetchCanvasCourseContentTool,
)
from agent_core.tools.automation_tools import (
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
from agent_core.tools.sjtu_jw_tools import FetchSjtuUndergradScheduleTool
from agent_core.tools.shuiyuan_tools import (
    ShuiyuanSearchTool,
    ShuiyuanGetTopicTool,
    ShuiyuanRetortTool,
    ShuiyuanPostReplyTool,
)
from agent_core.memory import ContentMemory, LongTermMemory


def get_default_tools(config: Optional[Config] = None) -> List[BaseTool]:
    """
    根据配置构建默认工具列表。

    Args:
        config: 配置对象，用于判断是否启用网页抓取、文件读写等工具

    Returns:
        工具实例列表
    """
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
        PlanTasksTool(planning_config=config.planning if config else None),
    ]

    if config and config.file_tools.enabled:
        tools.append(ReadFileTool(config=config))
        tools.append(WriteFileTool(config=config))
        tools.append(ModifyFileTool(config=config))

    if config and config.command_tools.enabled:
        tools.append(RunCommandTool(config=config))

    if config and (
        (config.skills.enabled or []) or getattr(config.skills, "cli_dir", None)
    ):
        tools.append(LoadSkillTool(config=config))

    if config and config.memory.enabled:
        from agent_core.agent.memory_paths import resolve_memory_owner_paths

        mem_cfg = config.memory
        user_id = os.getenv("SCHEDULE_USER_ID", "root").strip() or "root"
        source = os.getenv("SCHEDULE_SOURCE", "cli").strip() or "cli"
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

    if config and config.multimodal.enabled:
        tools.append(AttachMediaTool())
        tools.append(AttachImageToReplyTool())

    tools.append(SyncCanvasTool(config=config))
    tools.append(FetchCanvasOverviewTool(config=config))
    tools.append(FetchCanvasCourseContentTool(config=config))

    if config is not None:
        tools.append(
            FetchSjtuUndergradScheduleTool(
                cookies_path=config.sjtu_jw.cookies_path,
                config=config.sjtu_jw,
            )
        )
    else:
        tools.append(FetchSjtuUndergradScheduleTool())

    tools.append(SyncSourcesTool())
    tools.append(GetSyncStatusTool())

    if config and config.shuiyuan.enabled:
        tools.append(ShuiyuanSearchTool(config=config))
        tools.append(ShuiyuanGetTopicTool(config=config))
        tools.append(ShuiyuanRetortTool(config=config))
        tools.append(ShuiyuanPostReplyTool(config=config))

    tools.append(GetDigestTool())
    tools.append(NotifyOwnerTool(config=config))
    tools.append(ListNotificationsTool())
    tools.append(AckNotificationTool())
    tools.append(ConfigureAutomationPolicyTool())
    tools.append(GetAutomationActivityTool())
    tools.append(CreateScheduledJobTool())

    return tools
