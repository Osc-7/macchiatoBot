#!/usr/bin/env python3
"""
Schedule Agent CLI 入口

提供命令行交互界面，允许用户通过自然语言与日程管理 Agent 进行交互。

运行模式（由命令行参数决定）：
- 默认：通过 UNIX socket 连接长驻 automation daemon；若 daemon 未运行则报错退出。
- --local：在当前进程内直接启动 ScheduleAgent（直连模式），不依赖 daemon。
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Callable, Awaitable, List, Optional, cast

from agent.config import Config, get_config
from agent.automation import (
    AutomationCoreGateway,
    AutomationIPCClient,
    SessionCutPolicy,
    default_socket_path,
)
from agent.core import ScheduleAgent, ScheduleAgentAdapter
from agent.core.interfaces import AgentHooks, AgentRunInput
from agent.cli import run_interactive_loop
from agent.utils.session_logger import SessionLogger
from agent.core.tools import (
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
    AttachImageToReplyTool,
    SyncCanvasTool,
    FetchCanvasOverviewTool,
    FetchCanvasCourseContentTool,
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
from agent.core.memory import (
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

    # 记忆系统工具（与 Agent 使用相同的按 user_id 命名空间路径；
    # MEMORY.md 本身保持使用全局路径，避免出现多份长期偏好副本）
    if config and config.memory.enabled:
        mem_cfg = config.memory
        user_id = os.getenv("SCHEDULE_USER_ID", "root").strip() or "root"

        long_term_dir = str(Path(mem_cfg.long_term_dir) / user_id)
        content_dir = str(Path(mem_cfg.content_dir) / user_id)

        long_term = LongTermMemory(
            storage_dir=long_term_dir,
            memory_md_path=mem_cfg.memory_md_path,
            qmd_enabled=mem_cfg.qmd_enabled,
            qmd_command=mem_cfg.qmd_command,
        )
        content = ContentMemory(
            content_dir=content_dir,
            qmd_enabled=mem_cfg.qmd_enabled,
            qmd_command=mem_cfg.qmd_command,
        )
        top_n = mem_cfg.recall_top_n
        tools.append(MemorySearchLongTermTool(long_term, top_n))
        tools.append(MemorySearchContentTool(content, top_n))
        tools.append(MemoryStoreTool(content))
        tools.append(MemoryIngestTool(content))

    # 多模态媒体挂载工具（声明下一轮需要附带的图片/视频）+ 回复附图工具（把图片随回复发给用户）
    if config and config.multimodal.enabled:
        tools.append(AttachMediaTool())
        tools.append(AttachImageToReplyTool())

    # Canvas 工具（始终注册，便于 search_tools 发现；启用状态在工具内部校验）
    tools.append(SyncCanvasTool(config=config))
    tools.append(FetchCanvasOverviewTool(config=config))
    tools.append(FetchCanvasCourseContentTool(config=config))
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


def _parse_args(argv: List[str]) -> tuple[bool, List[str]]:
    """
    解析命令行参数，分离「是否直连模式」与剩余参数（用于单条命令）。

    Returns:
        (direct_mode, remaining_args): direct_mode 为 True 表示使用 --local 直连；
        remaining_args 为 [script_name, ...] 形式，其中 script_name 后为可选的单条命令。
    """
    parser = argparse.ArgumentParser(
        description="Schedule Agent CLI：默认连接 automation daemon，使用 --local 时在当前进程直连运行。"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="直连模式：在当前进程内直接启动 Agent，不连接 daemon（默认关闭）",
    )
    parser.add_argument(
        "command",
        nargs="*",
        help="单条要执行的命令（不传则进入交互模式）",
    )
    parsed, unknown = parser.parse_known_args(argv[1:] if len(argv) > 1 else [])
    direct_mode = parsed.local
    remaining = [argv[0]] + (getattr(parsed, "command", []) or []) + unknown
    return direct_mode, remaining


async def main_async(args: Optional[List[str]] = None):
    """
    异步主函数。

    Args:
        args: 命令行参数（默认 sys.argv）；会在此处解析 --local 与单条命令。
    """
    raw_args = args if args is not None else sys.argv
    if not raw_args:
        raw_args = ["main.py"]
    direct_mode, args = _parse_args(raw_args)

    # 加载配置
    config = _load_config()
    if config is None:
        return

    # 获取默认工具
    tools = get_default_tools(config=config)
    user_id = os.getenv("SCHEDULE_USER_ID", "root").strip() or "root"
    source = os.getenv("SCHEDULE_SOURCE", "cli").strip() or "cli"
    default_session_id = f"{source}:default"

    session_logger = None

    agent_ref = None
    ipc_socket = os.getenv("SCHEDULE_AUTOMATION_SOCKET", "").strip() or default_socket_path()

    try:
        if not direct_mode:
            # 默认：仅通过 daemon（IPC）；若 daemon 未运行则报错退出
            ipc_client = AutomationIPCClient(
                owner_id=user_id,
                source=source,
                socket_path=ipc_socket,
            )
            if not await ipc_client.ping():
                print(f"错误: 未连接到 automation daemon ({ipc_socket})")
                print("请先运行: python automation_daemon.py")
                print("若需在当前进程直连运行，请使用: python main.py --local")
                sys.exit(1)
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

        # 直连模式（--local）：在当前进程内启动 ScheduleAgent，不连接 daemon
        if config.logging.enable_session_log:
            session_logger = SessionLogger(
                log_dir=config.logging.session_log_dir,
                enable_detailed_log=config.logging.enable_detailed_log,
                max_system_prompt_log_len=config.logging.max_system_prompt_log_len,
            )
            session_logger.on_session_start()

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
                # 不在 factory 里调用 activate_session，由 gateway._create_session 根据
                # is_expired 状态决定 replay_messages_limit，避免全量历史被错误加载。
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
            await gateway.activate_primary_session()
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
            turn_count: int = 0
            total_usage: dict[str, int] | None = None
            if agent_ref:
                get_turn_count = getattr(agent_ref, "get_turn_count", None)
                if callable(get_turn_count):
                    get_turn_count_fn = cast(
                        Callable[[], int | Awaitable[int] | None],
                        get_turn_count,
                    )
                    maybe_turn = get_turn_count_fn()
                    if isinstance(maybe_turn, int):
                        turn_count = maybe_turn
                    elif maybe_turn is not None:
                        turn_count = int(await maybe_turn)

                get_token_usage = getattr(agent_ref, "get_token_usage", None)
                if callable(get_token_usage):
                    get_token_usage_fn = cast(
                        Callable[
                            [],
                            dict[str, int]
                            | Awaitable[dict[str, int] | None]
                            | None,
                        ],
                        get_token_usage,
                    )
                    maybe_usage = get_token_usage_fn()
                    if isinstance(maybe_usage, dict) or maybe_usage is None:
                        total_usage = maybe_usage
                    else:
                        total_usage = await maybe_usage
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
