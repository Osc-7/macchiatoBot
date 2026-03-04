#!/usr/bin/env python3
"""
Schedule Agent CLI 入口

提供命令行交互界面，允许用户通过自然语言与日程管理 Agent 进行交互。
"""

import asyncio
import os
import sys
from typing import Any, Callable, Awaitable, List, Optional, cast

from schedule_agent.config import Config, get_config
from schedule_agent.automation import (
    AutomationCoreGateway,
    AutomationIPCClient,
    SessionCutPolicy,
    default_socket_path,
)
from schedule_agent.core import ScheduleAgent, ScheduleAgentAdapter
from schedule_agent.core.interfaces import AgentHooks, AgentRunInput
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
    AttachMediaTool,
    SyncCanvasTool,
    SyncSourcesTool,
    GetSyncStatusTool,
    GetDigestTool,
    ListNotificationsTool,
    AckNotificationTool,
    ConfigureAutomationPolicyTool,
    GetAutomationActivityTool,
    FetchSjtuUndergradScheduleTool,
    CreateScheduledJobTool,
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

    # 多模态媒体挂载工具（声明下一轮需要附带的图片/视频）
    if config and config.multimodal.enabled:
        tools.append(AttachMediaTool())

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
    tools.append(CreateScheduledJobTool())

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


async def run_single_command(agent: Any, command: str) -> str:
    """
    执行单条命令。

    Args:
        agent: ScheduleAgent 实例
        command: 命令字符串

    Returns:
        Agent 的响应
    """
    if hasattr(agent, "run_turn"):
        result = await agent.run_turn(AgentRunInput(text=command), hooks=AgentHooks())
        return result.output_text
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
    user_id = os.getenv("SCHEDULE_USER_ID", "root").strip() or "root"
    source = os.getenv("SCHEDULE_SOURCE", "cli").strip() or "cli"
    default_session_id = f"{source}:default"

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
    use_ipc_mode = (os.getenv("SCHEDULE_AUTOMATION_IPC", "auto").strip() or "auto").lower()
    ipc_socket = os.getenv("SCHEDULE_AUTOMATION_SOCKET", "").strip() or default_socket_path()
    try:
        if use_ipc_mode in {"1", "true", "yes", "auto"}:
            ipc_client = AutomationIPCClient(
                owner_id=user_id,
                source=source,
                socket_path=ipc_socket,
            )
            if await ipc_client.ping():
                await ipc_client.connect()
                agent_ref = ipc_client
                try:
                    if args and len(args) > 1:
                        command = " ".join(args[1:])
                        response = await run_single_command(ipc_client, command)
                        print(response)
                    else:
                        await run_interactive_loop(ipc_client)
                finally:
                    await ipc_client.close()
                return
            if use_ipc_mode in {"1", "true", "yes"}:
                print(f"错误: 未连接到 automation daemon ({ipc_socket})")
                print("请先运行: python automation_daemon.py")
                sys.exit(1)

        async with ScheduleAgent(
            config=config,
            tools=tools,
            max_iterations=config.agent.max_iterations,
            timezone=config.time.timezone,
            session_logger=session_logger,
            user_id=user_id,
            source=source,
        ) as agent:
            core_session = ScheduleAgentAdapter(agent)
            async def _build_core_session(session_key: str) -> ScheduleAgentAdapter:
                # 新会话使用独立 Agent，确保多会话上下文隔离。
                created_agent = ScheduleAgent(
                    config=config,
                    tools=tools,
                    max_iterations=config.agent.max_iterations,
                    timezone=config.time.timezone,
                    session_logger=session_logger,
                    user_id=user_id,
                    source=source,
                )
                await created_agent.__aenter__()
                adapter = ScheduleAgentAdapter(created_agent)
                await adapter.activate_session(session_key)
                return adapter
            try:
                idle_timeout = int(config.memory.idle_timeout_minutes)
            except Exception:
                idle_timeout = 30
            gateway = AutomationCoreGateway(
                core_session,
                session_id=default_session_id,
                policy=SessionCutPolicy(
                    idle_timeout_minutes=idle_timeout,
                    daily_cutoff_hour=4,
                ),
                session_factory=_build_core_session,
                owner_id=user_id,
                source=source,
            )
            await core_session.activate_session(default_session_id)
            agent_ref = gateway
            try:
                # 检查是否有命令行参数
                if args and len(args) > 1:
                    # 执行单条命令
                    command = " ".join(args[1:])
                    response = await run_single_command(gateway, command)
                    print(response)
                else:
                    # 运行交互式循环
                    await run_interactive_loop(gateway)
            finally:
                if agent_ref:
                    try:
                        await agent_ref.close()
                    except Exception:
                        pass
    finally:
        if session_logger:
            turn_count = 0
            total_usage = None
            if agent_ref:
                get_turn_count = getattr(agent_ref, "get_turn_count", None)
                if callable(get_turn_count):
                    maybe_turn = get_turn_count()
                    if hasattr(maybe_turn, "__await__"):
                        turn_count = int(await maybe_turn)
                    else:
                        turn_count = int(maybe_turn or 0)

                get_token_usage = getattr(agent_ref, "get_token_usage", None)
                if callable(get_token_usage):
                    maybe_usage = get_token_usage()
                    if hasattr(maybe_usage, "__await__"):
                        total_usage = await maybe_usage
                    else:
                        total_usage = maybe_usage
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
