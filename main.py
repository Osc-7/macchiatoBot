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
from schedule_agent.core.tools import (
    BaseTool,
    ParseTimeTool,
    AddEventTool,
    AddTaskTool,
    GetEventsTool,
    GetTasksTool,
    GetFreeSlotsTool,
    PlanTasksTool,
)


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
        GetFreeSlotsTool(),
        PlanTasksTool(),
    ]


def print_welcome():
    """打印欢迎信息"""
    print("\n" + "=" * 50)
    print("  Schedule Agent - 智能日程管理助手")
    print("=" * 50)
    print("\n你好！我是你的日程管理助手，可以帮助你：")
    print("  - 添加日程事件（会议、约会等）")
    print("  - 创建待办任务")
    print("  - 查询日程和任务")
    print("  - 智能规划时间")
    print("\n输入 'quit' 或 'exit' 退出程序")
    print("输入 'clear' 清空对话历史")
    print("输入 'help' 查看帮助信息")
    print("-" * 50 + "\n")


def print_help():
    """打印帮助信息"""
    print("\n" + "-" * 50)
    print("帮助信息")
    print("-" * 50)
    print("\n可用命令:")
    print("  quit / exit  - 退出程序")
    print("  clear        - 清空对话历史")
    print("  help         - 显示此帮助信息")
    print("\n示例对话:")
    print("  - 明天下午3点有个团队会议")
    print("  - 添加一个任务：完成项目报告，预计2小时，周五前完成")
    print("  - 查看今天的日程")
    print("  - 查看我的待办任务")
    print("  - 帮我规划一下明天的任务")
    print("-" * 50 + "\n")


async def run_interactive_loop(agent: ScheduleAgent):
    """
    运行交互式对话循环。

    Args:
        agent: ScheduleAgent 实例
    """
    print_welcome()

    while True:
        try:
            # 读取用户输入
            user_input = input("你: ").strip()

            # 跳过空输入
            if not user_input:
                continue

            # 处理退出命令
            if user_input.lower() in ("quit", "exit", "q"):
                print("\n再见！祝你生活愉快！\n")
                break

            # 处理清空对话命令
            if user_input.lower() == "clear":
                agent.clear_context()
                print("\n对话历史已清空。\n")
                continue

            # 处理帮助命令
            if user_input.lower() == "help":
                print_help()
                continue

            # 处理用户输入
            try:
                response = await agent.process_input(user_input)
                print(f"\n助手: {response}\n")
            except Exception as e:
                print(f"\n抱歉，处理您的请求时发生错误: {str(e)}\n")
                print("请重试或换一种方式表达。\n")

        except KeyboardInterrupt:
            print("\n\n检测到中断信号，正在退出...\n")
            break
        except EOFError:
            print("\n\n再见！\n")
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

    # 创建 Agent
    async with ScheduleAgent(
        config=config,
        tools=tools,
        max_iterations=config.agent.max_iterations,
        timezone=config.time.timezone,
    ) as agent:
        # 检查是否有命令行参数
        if args and len(args) > 1:
            # 执行单条命令
            command = " ".join(args[1:])
            response = await run_single_command(agent, command)
            print(response)
        else:
            # 运行交互式循环
            await run_interactive_loop(agent)


def main():
    """CLI 入口点"""
    asyncio.run(main_async(sys.argv))


if __name__ == "__main__":
    main()
