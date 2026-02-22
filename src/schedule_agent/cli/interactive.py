"""
CLI 交互式界面

包含欢迎信息、帮助、token 用量展示以及主交互循环。
"""

import asyncio
import sys
import shutil
from typing import Optional

from schedule_agent.core import ScheduleAgent
from schedule_agent.utils.cli_style import (
    hint,
    label,
    accent,
    prompt_prefix,
    thin_separator,
    status_bar,
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


async def _thinking_spinner(stop_event: "asyncio.Event") -> None:
    """
    简单的「正在思考」动画。

    使用单行覆盖的方式，不依赖 prompt_toolkit，只在等待 LLM 响应期间运行。
    """
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    i = 0
    # 预估一行宽度，用于清理时覆盖整行（避免中英文宽度差导致残留）
    width = shutil.get_terminal_size((80, 20)).columns
    while not stop_event.is_set():
        prefix = frames[i % len(frames)]
        msg = f"{prefix} 正在思考，请稍候…"
        sys.stdout.write("\r" + msg)
        sys.stdout.flush()
        i += 1
        try:
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            break

    # 清空这一行（用整行宽度覆盖，避免多字节字符残留）
    sys.stdout.write("\r" + " " * width + "\r")
    sys.stdout.flush()


def print_welcome():
    """打印欢迎信息（Markdown 格式，推荐 rich 渲染）"""
    md = """
    ╔══════════════════════════════════════════════════════╗
    ║ Greetings!                                           ║
    ╟──────────────────────────────────────────────────────╢
    ║ ░█▀▄░█──░█░█─▄─▄─░█▀▄─▄▀▀                            ║
    ║ ░█─█░█──░█░█─█─█─░█─█─▀▀▄                            ║
    ║ ░█▄▀─▀▀──▀─▀─▀─▀─░█▀─░▀▀▀                            ║
    ║                                 MACCHIATO            ║
    ╚══════════════════════════════════════════════════════╝"""
    if _HAS_RICH and _RICH_CONSOLE is not None:
        _RICH_CONSOLE.print(Markdown(md))
    else:
        print()
        print("=" * 50)
        print("  Schedule Agent - 智能日程管理助手")
        print("=" * 50)
        print()
        print("你好！我是你的日程管理助手，可以帮助你：")
        print("  • 添加日程事件（会议、约会等）")
        print("  • 创建待办任务")
        print("  • 查询日程和任务")
        print("  • 智能规划时间")
        print()
        print("命令： quit/exit 退出  |  clear 清空对话  |  help 帮助  |  usage/stats 用量")
        print("-" * 50)
        print()


def print_help():
    """打印帮助信息（Markdown 格式，推荐 rich 渲染）"""
    md = """
# 帮助信息

## 可用命令

- `quit` / `exit` &nbsp;&nbsp;退出程序
- `clear` &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;清空对话历史
- `help` &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;显示此帮助
- `usage` / `stats` &nbsp;&nbsp;本会话 token 用量

## 示例对话

- 明天下午3点有个团队会议
- 添加一个任务：完成项目报告，预计2小时，周五前完成
- 查看今天的日程
- 查看我的待办任务
- 帮我规划一下明天的任务
"""
    if _HAS_RICH and _RICH_CONSOLE is not None:
        _RICH_CONSOLE.print(Markdown(md))
    else:
        print()
        print("=" * 50)
        print("  帮助信息")
        print("=" * 50)
        print()
        print("可用命令:")
        print("  quit / exit  退出程序")
        print("  clear       清空对话历史")
        print("  help        显示此帮助")
        print("  usage/stats 本会话 token 用量")
        print()
        print("示例对话:")
        print("  • 明天下午3点有个团队会议")
        print("  • 添加一个任务：完成项目报告，预计2小时，周五前完成")
        print("  • 查看今天的日程")
        print("  • 查看我的待办任务")
        print("  • 帮我规划一下明天的任务")
        print("-" * 50)
        print()


def print_token_usage(agent: ScheduleAgent):
    """打印本会话 token 用量统计（Markdown 格式，推荐 rich 渲染）"""
    u = agent.get_token_usage()
    md = f"""
# Token 用量统计

- **调用次数**: `{u['call_count']}`
- **输入 token**: `{u['prompt_tokens']}`
- **输出 token**: `{u['completion_tokens']}`
- **合计 token**: `{u['total_tokens']}`
"""
    if _HAS_RICH and _RICH_CONSOLE is not None:
        _RICH_CONSOLE.print(Markdown(md))
    else:
        print()
        print("=" * 50)
        print("  本会话 Token 用量统计")
        print("=" * 50)
        print(f"  调用次数:     {u['call_count']}")
        print(f"  输入 token:   {u['prompt_tokens']}")
        print(f"  输出 token:   {u['completion_tokens']}")
        print(f"  合计 token:   {u['total_tokens']}")
        print("=" * 50)
        print()


async def run_interactive_loop(agent: ScheduleAgent):
    """
    运行交互式对话循环。

    Args:
        agent: ScheduleAgent 实例
    """
    print_welcome()
    print(thin_separator())

    if _HAS_PROMPT_TOOLKIT:
        pt_session = _PromptSession()
        pt_prompt = HTML("<style fg='ansicyan' bold='true'>❯ </style>")
    else:
        pt_session = None
        pt_prompt = None

    # 记录上一轮的 token 总量，用于计算增量
    prev_total_tokens = 0

    while True:
        try:
            # 获取用户输入
            if pt_session is not None and pt_prompt is not None:
                user_input = (
                    await pt_session.prompt_async(pt_prompt)
                ).strip()
            else:
                user_input = input(prompt_prefix()).strip()

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

            if user_input.lower() == "clear":
                agent.clear_context()
                print(hint("  对话历史已清空。"))
                print(thin_separator())
                continue

            if user_input.lower() == "help":
                print_help()
                print(thin_separator())
                continue

            if user_input.lower() in ("usage", "stats", "tokens"):
                print_token_usage(agent)
                print(thin_separator())
                continue

            # 处理用户输入
            spinner_stop: Optional[asyncio.Event] = None
            spinner_task: Optional["asyncio.Task"] = None
            try:
                spinner_stop = asyncio.Event()
                spinner_task = asyncio.create_task(_thinking_spinner(spinner_stop))

                response = await agent.process_input(user_input)

                spinner_stop.set()
                await spinner_task

                # 输出助手响应
                print()
                if _HAS_RICH and _RICH_CONSOLE is not None:
                    _RICH_CONSOLE.print(Markdown(response))
                else:
                    print(response)
                print()

                # 状态栏：token 用量
                u = agent.get_token_usage()
                delta = u["total_tokens"] - prev_total_tokens
                prev_total_tokens = u["total_tokens"]
                print(status_bar(u["total_tokens"], u["call_count"], delta))

            except Exception as e:
                if spinner_stop is not None:
                    spinner_stop.set()
                if spinner_task is not None:
                    try:
                        await spinner_task
                    except Exception:
                        pass

                print()
                print(accent("  抱歉，处理您的请求时发生错误: ") + str(e))
                print(hint("  请重试或换一种方式表达。"))
                print(thin_separator())

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
