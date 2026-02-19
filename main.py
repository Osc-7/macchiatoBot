#!/usr/bin/env python3
"""
Schedule Agent CLI 入口

提供命令行交互界面，允许用户通过自然语言与日程管理 Agent 进行交互。
"""

import asyncio
import sys
from typing import List, Optional

from schedule_agent.config import Config, get_config
from schedule_agent.core import ScheduleAgent
from schedule_agent.utils.session_logger import SessionLogger
from schedule_agent.utils.cli_style import (
    title,
    hint,
    label,
    accent,
    prompt_prefix,
    assistant_prefix,
    sep_line,
)
from schedule_agent.core.tools import (
    BaseTool,
    ParseTimeTool,
    AddEventTool,
    AddTaskTool,
    GetEventsTool,
    GetTasksTool,
    UpdateTaskTool,
    DeleteScheduleDataTool,
    GetFreeSlotsTool,
    PlanTasksTool,
)

try:
    from prompt_toolkit import PromptSession as _PromptSession
    from prompt_toolkit.formatted_text import HTML
    _HAS_PROMPT_TOOLKIT = True
except ImportError:
    _HAS_PROMPT_TOOLKIT = False

try:
    from rich.console import Console
    from rich.markdown import Markdown
    _HAS_RICH = True
    _RICH_CONSOLE: Optional["Console"] = Console()
except Exception:  # pragma: no cover - 极端环境下容错
    _HAS_RICH = False
    _RICH_CONSOLE = None


def get_default_tools() -> List[BaseTool]:
    """
    获取默认的工具列表。

    Returns:
        工具实例列表
    """
    return [
        ParseTimeTool(),
        AddEventTool(),
        AddTaskTool(),
        GetEventsTool(),
        GetTasksTool(),
        UpdateTaskTool(),
        DeleteScheduleDataTool(),
        GetFreeSlotsTool(),
        PlanTasksTool(),
    ]


def print_welcome():
    """打印欢迎信息"""
    print()
    print(sep_line("=", 50))
    print("  " + title("Schedule Agent - 智能日程管理助手"))
    print(sep_line("=", 50))
    print()
    print(hint("你好！我是你的日程管理助手，可以帮助你："))
    print("  • 添加日程事件（会议、约会等）")
    print("  • 创建待办任务")
    print("  • 查询日程和任务")
    print("  • 智能规划时间")
    print()
    print(hint("命令：") + " quit/exit 退出  |  clear 清空对话  |  help 帮助  |  usage/stats 用量")
    print(sep_line("-", 50))
    print()


def print_help():
    """打印帮助信息"""
    print()
    print(sep_line("-", 50))
    print(title("帮助信息"))
    print(sep_line("-", 50))
    print()
    print(label("可用命令:"))
    print("  quit / exit  退出程序")
    print("  clear       清空对话历史")
    print("  help        显示此帮助")
    print("  usage/stats 本会话 token 用量")
    print()
    print(label("示例对话:"))
    print("  • 明天下午3点有个团队会议")
    print("  • 添加一个任务：完成项目报告，预计2小时，周五前完成")
    print("  • 查看今天的日程")
    print("  • 查看我的待办任务")
    print("  • 帮我规划一下明天的任务")
    print(sep_line("-", 50))
    print()


def print_token_usage(agent: ScheduleAgent):
    """打印本会话 token 用量统计"""
    u = agent.get_token_usage()
    print()
    print(sep_line("-", 50))
    print(title("本会话 Token 用量统计"))
    print(sep_line("-", 50))
    print(f"  调用次数:     {accent(str(u['call_count']))}")
    print(f"  输入 token:   {u['prompt_tokens']}")
    print(f"  输出 token:   {u['completion_tokens']}")
    print(f"  合计 token:   {u['total_tokens']}")
    print(sep_line("-", 50))
    print()


async def run_interactive_loop(agent: ScheduleAgent):
    """
    运行交互式对话循环。

    Args:
        agent: ScheduleAgent 实例
    """
    print_welcome()

    # 有 prompt_toolkit 时使用带样式的提示符和底部状态栏显示 token 统计
    if _HAS_PROMPT_TOOLKIT:
        pt_session = _PromptSession()
        prompt_text = HTML('<style fg="ansicyan" bold="true">你: </style>')

        def _bottom_toolbar():
            """底部状态栏：显示当前会话的 token 统计信息"""
            u = agent.get_token_usage()
            # 使用浅色背景 + 深色前景，类似 Vim 状态栏效果
            text = (
                f" Token: total {u['total_tokens']} "
                f"| in {u['prompt_tokens']} "
                f"| out {u['completion_tokens']} "
                f"| calls {u['call_count']} "
            )
            return HTML(
                "<style bg='ansigray' fg='ansiblack'>"
                + text +
                "</style>"
            )
    else:
        pt_session = None
        prompt_text = None

    while True:
        try:
            if pt_session is not None and prompt_text is not None:
                user_input = (
                    await pt_session.prompt_async(
                        prompt_text,
                        bottom_toolbar=_bottom_toolbar,
                    )
                ).strip()
            else:
                user_input = input(prompt_prefix()).strip()

            # 跳过空输入
            if not user_input:
                continue

            # 处理退出命令
            if user_input.lower() in ("quit", "exit", "q"):
                u = agent.get_token_usage()
                if u["call_count"] > 0:
                    print()
                    print(hint(f"本会话共调用 LLM {u['call_count']} 次，合计 token: {u['total_tokens']}（输入 {u['prompt_tokens']} + 输出 {u['completion_tokens']}）"))
                print()
                print(label("再见！祝你生活愉快！"))
                print()
                break

            # 处理清空对话命令
            if user_input.lower() == "clear":
                agent.clear_context()
                print()
                print(hint("对话历史已清空。"))
                print()
                continue

            # 处理帮助命令
            if user_input.lower() == "help":
                print_help()
                continue

            # 处理 token 用量统计
            if user_input.lower() in ("usage", "stats", "tokens"):
                print_token_usage(agent)
                continue

            # 处理用户输入
            try:
                response = await agent.process_input(user_input)
                print()
                # 优先使用 rich 渲染 Markdown，以获得更好的终端展示效果
                if _HAS_RICH and _RICH_CONSOLE is not None:
                    # 将「助手」前缀和内容一起作为 Markdown 渲染，避免前缀与正文错位
                    md_text = f"**助手：**\n\n{response}"
                    _RICH_CONSOLE.print(Markdown(md_text))
                else:
                    print(assistant_prefix() + response)
                print()
            except Exception as e:
                print()
                print(accent("抱歉，处理您的请求时发生错误: ") + str(e))
                print(hint("请重试或换一种方式表达。"))
                print()

        except KeyboardInterrupt:
            print()
            print(hint("检测到中断信号，正在退出..."))
            print()
            break
        except EOFError:
            print()
            print(label("再见！"))
            print()
            break


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
    tools = get_default_tools()

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
            # 检查是否有命令行参数
            if args and len(args) > 1:
                # 执行单条命令
                command = " ".join(args[1:])
                response = await run_single_command(agent, command)
                print(response)
            else:
                # 运行交互式循环
                await run_interactive_loop(agent)
    finally:
        if session_logger:
            turn_count = agent_ref.get_turn_count() if agent_ref else 0
            total_usage = agent_ref.get_token_usage() if agent_ref else None
            session_logger.on_session_end(turn_count, total_usage)
            session_logger.close()


def main():
    """CLI 入口点"""
    asyncio.run(main_async(sys.argv))


if __name__ == "__main__":
    main()
