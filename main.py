#!/usr/bin/env python3
"""
Schedule Agent CLI 入口

提供命令行交互界面，允许用户通过自然语言与日程管理 Agent 进行交互。
"""

import asyncio
import sys
from typing import Callable, Awaitable, List, Optional, cast

from schedule_agent.config import Config, get_config
from schedule_agent.core import ScheduleAgent
from schedule_agent.cli import run_interactive_loop
from schedule_agent.utils.session_logger import SessionLogger
from schedule_agent.core.tools import (
    BaseTool,
    LoadSkillTool,
    ParseTimeTool,
    AddEventTool,
    AddTaskTool,
    GetEventsTool,
    GetTasksTool,
    UpdateEventTool,
    UpdateTaskTool,
    DeleteScheduleDataTool,
    ReadFileTool,
    WriteFileTool,
    ModifyFileTool,
    GetFreeSlotsTool,
    PlanTasksTool,
    RunCommandTool,
    MemorySearchLongTermTool,
    MemorySearchContentTool,
    MemoryStoreTool,
    MemoryIngestTool,
    AnalyzeImageTool,
    SyncCanvasTool,
    SyncSourcesTool,
    GetSyncStatusTool,
    GetDigestTool,
    ListNotificationsTool,
    AckNotificationTool,
    ConfigureAutomationPolicyTool,
    GetAutomationActivityTool,
    FetchSjtuUndergradScheduleTool,
)
from schedule_agent.core.memory import (
    ContentMemory,
    LongTermMemory,
)


def get_default_tools(config: Optional[Config] = None) -> List[BaseTool]:
    """
    获取默认的工具列表。

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

    # 文件读写工具（读取、写入、修改，写入/修改需配置允许）
    if config and config.file_tools.enabled:
        tools.append(ReadFileTool(config=config))
        tools.append(WriteFileTool(config=config))
        tools.append(ModifyFileTool(config=config))

    # 终端命令执行工具
    if config and config.command_tools.enabled:
        tools.append(RunCommandTool(config=config))

    # 联网工具（在启用 MCP 时由 Agent 内部注册）

    # 技能按需加载工具（skills.enabled 或 skills.cli_dir 时注册）
    if config and ((config.skills.enabled or []) or getattr(config.skills, "cli_dir", None)):
        tools.append(LoadSkillTool(config=config))

    # 记忆系统工具
    if config and config.memory.enabled:
        mem_cfg = config.memory
        long_term = LongTermMemory(mem_cfg.long_term_dir, mem_cfg.memory_md_path)
        content = ContentMemory(
            mem_cfg.content_dir, mem_cfg.qmd_enabled, mem_cfg.qmd_command
        )
        top_n = mem_cfg.recall_top_n
        tools.append(MemorySearchLongTermTool(long_term, top_n))
        tools.append(MemorySearchContentTool(content, top_n))
        tools.append(MemoryStoreTool(content))
        tools.append(MemoryIngestTool(content))

    # 多模态识图工具（调用多模态大模型）
    if config and config.multimodal.enabled:
        tools.append(AnalyzeImageTool(config=config))

    # Canvas 同步工具（始终注册，便于 search_tools 发现；启用状态在工具内部校验）
    tools.append(SyncCanvasTool(config=config))
    # 交大教学信息服务网课表同步工具（基于 Cookie，只读）
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
    tools.append(GetDigestTool())
    tools.append(ListNotificationsTool())
    tools.append(AckNotificationTool())
    tools.append(ConfigureAutomationPolicyTool())
    tools.append(GetAutomationActivityTool())

    return tools


def _load_config() -> Optional[Config]:
    """
    加载配置文件。

    Returns:
        Config 对象，如果加载失败返回 None
    """
    try:
        return get_config()
    except FileNotFoundError as e:
        print(f"错误: {str(e)}")
        print("请确保 config.yaml 文件存在并正确配置。")
        sys.exit(1)
    except Exception as e:
        print(f"加载配置失败: {str(e)}")
        sys.exit(1)


async def run_single_command(agent: ScheduleAgent, command: str) -> str:
    """
    执行单条命令。

    Args:
        agent: ScheduleAgent 实例
        command: 命令字符串

    Returns:
        Agent 的响应
    """
    return await agent.process_input(command)


async def main_async(args: Optional[List[str]] = None):
    """
    异步主函数。

    Args:
        args: 命令行参数
    """
    # 加载配置
    config = _load_config()
    if config is None:
        return

    # 获取默认工具
    tools = get_default_tools(config=config)

    # 创建 Session 日志记录器（若启用）
    session_logger = None
    if config.logging.enable_session_log:
        session_logger = SessionLogger(
            log_dir=config.logging.session_log_dir,
            enable_detailed_log=config.logging.enable_detailed_log,
            max_system_prompt_log_len=config.logging.max_system_prompt_log_len,
        )
        session_logger.on_session_start()

    agent_ref = None
    try:
        async with ScheduleAgent(
            config=config,
            tools=tools,
            max_iterations=config.agent.max_iterations,
            timezone=config.time.timezone,
            session_logger=session_logger,
        ) as agent:
            agent_ref = agent
            should_finalize_session = True
            try:
                # 检查是否有命令行参数
                if args and len(args) > 1:
                    # 执行单条命令
                    command = " ".join(args[1:])
                    response = await run_single_command(agent, command)
                    print(response)
                else:
                    # 运行交互式循环
                    exit_reason = await run_interactive_loop(agent)
                    should_finalize_session = exit_reason == "quit"
            finally:
                # 会话结束时触发记忆总结（必须在 LLM close 之前，因为需要调用 LLM）
                if agent_ref and should_finalize_session:
                    try:
                        await agent_ref.finalize_session()
                    except Exception:
                        pass  # 静默处理，避免干扰终端
    finally:
        if session_logger:
            turn_count = agent_ref.get_turn_count() if agent_ref else 0
            total_usage = agent_ref.get_token_usage() if agent_ref else None
            session_logger.on_session_end(turn_count, total_usage)
            session_logger.close()


def main():
    """CLI 入口点"""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main_async(sys.argv))
    except (KeyboardInterrupt, asyncio.CancelledError):
        # 顶层兜底：避免事件循环边界的中断直接打出 traceback。
        return
    finally:
        try:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            shutdown_default_executor = getattr(loop, "shutdown_default_executor", None)
            if shutdown_default_executor is not None:
                shutdown_executor_fn = cast(Callable[[], Awaitable[None]], shutdown_default_executor)
                loop.run_until_complete(shutdown_executor_fn())
        except Exception:
            pass
        finally:
            asyncio.set_event_loop(None)
            loop.close()


if __name__ == "__main__":
    main()
