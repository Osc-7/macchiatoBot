#!/usr/bin/env python3
"""
Schedule Agent CLI 入口

通过 UNIX socket 连接长驻 automation daemon，提供命令行交互界面。
若 daemon 未运行则报错退出。
"""

import argparse
import asyncio
import os
import sys
from typing import Any, Awaitable, Callable, List, Optional, cast

from agent_core.config import get_config, Config
from agent_core.interfaces import AgentHooks, AgentRunInput
from system.automation import AutomationIPCClient, default_socket_path
from frontend.cli import run_interactive_loop


def _load_config() -> Config:
    """加载配置文件，失败时退出进程。"""
    try:
        return get_config()
    except FileNotFoundError as e:
        print(f"错误: {str(e)}")
        print("请确保 config.yaml 文件存在并正确配置。")
        sys.exit(1)
    except Exception as e:
        print(f"加载配置失败: {str(e)}")
        sys.exit(1)


def _parse_args(argv: List[str]) -> List[str]:
    """
    解析命令行参数。

    Returns:
        remaining_args: [script_name, ...] 形式，其中 script_name 后为可选的单条命令。
    """
    parser = argparse.ArgumentParser(description="Schedule Agent CLI")
    parser.add_argument(
        "command",
        nargs="*",
        help="单条要执行的命令（不传则进入交互模式）",
    )
    parsed, unknown = parser.parse_known_args(argv[1:] if len(argv) > 1 else [])
    return [argv[0]] + (getattr(parsed, "command", []) or []) + unknown


async def run_single_command(agent: Any, command: str) -> str:
    """执行单条命令并返回响应文本。要求 agent 实现 run_turn。"""
    result = await agent.run_turn(AgentRunInput(text=command), hooks=AgentHooks())
    return result.output_text


async def _dispatch(agent: Any, args: List[str]) -> None:
    """根据命令行参数决定执行单条命令还是进入交互循环。"""
    if args and len(args) > 1:
        command = " ".join(args[1:])
        response = await run_single_command(agent, command)
        print(response)
    else:
        await run_interactive_loop(agent)


async def main_async(args: Optional[List[str]] = None):
    """异步主函数：连接 daemon 并运行 CLI。"""
    raw_args = args if args is not None else sys.argv
    if not raw_args:
        raw_args = ["main.py"]
    remaining = _parse_args(raw_args)

    _load_config()

    user_id = os.getenv("SCHEDULE_USER_ID", "root").strip() or "root"
    source = os.getenv("SCHEDULE_SOURCE", "cli").strip() or "cli"
    ipc_socket = (
        os.getenv("SCHEDULE_AUTOMATION_SOCKET", "").strip() or default_socket_path()
    )

    ipc_client = AutomationIPCClient(
        owner_id=user_id,
        source=source,
        socket_path=ipc_socket,
    )
    if not await ipc_client.ping():
        print(f"错误: 未连接到 automation daemon ({ipc_socket})")
        print("请先运行: python automation_daemon.py")
        sys.exit(1)

    await ipc_client.connect()
    try:
        await _dispatch(ipc_client, remaining)
    finally:
        await ipc_client.close()


def main():
    """CLI 入口点"""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main_async(sys.argv))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return
    finally:
        try:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            shutdown_default_executor = getattr(loop, "shutdown_default_executor", None)
            if shutdown_default_executor is not None:
                shutdown_executor_fn = cast(
                    Callable[[], Awaitable[None]], shutdown_default_executor
                )
                loop.run_until_complete(shutdown_executor_fn())
        except Exception:
            pass
        finally:
            asyncio.set_event_loop(None)
            loop.close()


if __name__ == "__main__":
    main()
