"""
主 Agent 实现

实现基于工具驱动的 Agent 循环，支持多轮对话和工具调用。
"""

import json
from typing import Any, Dict, List, Optional

from schedule_agent.config import Config, get_config
from schedule_agent.core.context import ConversationContext, get_time_context
from schedule_agent.core.llm import LLMClient, LLMResponse, ToolCall, TokenUsage
from schedule_agent.core.tools import BaseTool, ToolRegistry, ToolResult


class ScheduleAgent:
    """
    日程管理 Agent。

    基于 LLM 的智能日程管理助手，支持：
    - 自然语言交互
    - 多轮对话
    - 工具调用（添加事件、任务、查询等）
    - 时间上下文感知
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        tools: Optional[List[BaseTool]] = None,
        max_iterations: int = 10,
        timezone: str = "Asia/Shanghai",
    ):
        """
        初始化 Agent。

        Args:
            config: 配置对象，如果为 None 则使用全局配置
            tools: 工具列表，如果为 None 则使用空注册表
            max_iterations: 最大工具调用迭代次数
            timezone: 时区
        """
        self._config = config or get_config()
        self._llm_client = LLMClient(self._config)
        self._tool_registry = ToolRegistry()
        self._context = ConversationContext()
        self._max_iterations = max_iterations
        self._timezone = timezone
        # 本会话 token 用量累计
        self._token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "call_count": 0}

        # 注册工具
        if tools:
            for tool in tools:
                self._tool_registry.register(tool)

    @property
    def tool_registry(self) -> ToolRegistry:
        """获取工具注册表"""
        return self._tool_registry

    @property
    def context(self) -> ConversationContext:
        """获取对话上下文"""
        return self._context

    def register_tool(self, tool: BaseTool) -> None:
        """
        注册工具。

        Args:
            tool: 工具实例
        """
        self._tool_registry.register(tool)

    def unregister_tool(self, name: str) -> bool:
        """
        注销工具。

        Args:
            name: 工具名称

        Returns:
            是否成功注销
        """
        return self._tool_registry.unregister(name)

    def clear_context(self) -> None:
        """清空对话上下文"""
        self._context.clear()

    def get_token_usage(self) -> dict:
        """
        获取本会话累计的 token 用量。

        Returns:
            包含 prompt_tokens, completion_tokens, total_tokens, call_count 的字典
        """
        return dict(self._token_usage)

    def reset_token_usage(self) -> None:
        """重置本会话的 token 用量统计"""
        self._token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "call_count": 0}

    async def process_input(self, user_input: str) -> str:
        """
        处理用户输入。

        这是 Agent 的主入口点，实现了工具驱动的对话循环：
        1. 添加用户消息到上下文
        2. 调用 LLM 获取响应
        3. 如果有工具调用，执行工具并继续循环
        4. 返回最终响应

        Args:
            user_input: 用户输入

        Returns:
            Agent 的响应文本
        """
        # 1. 添加用户消息到上下文
        self._context.add_user_message(user_input)

        # 2. Agent 主循环
        iteration = 0
        while iteration < self._max_iterations:
            iteration += 1

            # 2.1 调用 LLM
            response = await self._llm_client.chat_with_tools(
                system_message=self._build_system_prompt(),
                messages=self._context.get_messages(),
                tools=self._tool_registry.get_all_definitions(),
                tool_choice="auto",
            )

            # 累计 token 用量
            if response.usage:
                self._token_usage["prompt_tokens"] += response.usage.prompt_tokens
                self._token_usage["completion_tokens"] += response.usage.completion_tokens
                self._token_usage["total_tokens"] += response.usage.total_tokens
                self._token_usage["call_count"] += 1

            # 2.2 处理工具调用
            if response.tool_calls:
                # 添加助手消息（包含工具调用）
                self._add_assistant_message_with_tool_calls(response)

                # 执行所有工具调用
                for tool_call in response.tool_calls:
                    result = await self._execute_tool_call(tool_call)
                    self._context.add_tool_result(tool_call.id, result)

                # 继续循环，让 LLM 处理工具结果
                continue

            # 2.3 返回最终响应
            if response.content:
                self._context.add_assistant_message(content=response.content)
                return response.content

            # 如果没有内容也没有工具调用，返回默认响应
            return "抱歉，我无法处理您的请求。请重试或换一种方式表达。"

        # 超过最大迭代次数
        return "抱歉，处理您的请求时超出了最大迭代次数。请简化您的问题或稍后重试。"

    def _build_system_prompt(self) -> str:
        """
        构建系统提示。

        包含：
        - Agent 身份和能力说明
        - 当前时间上下文
        - 工具使用指南

        Returns:
            系统提示字符串
        """
        time_ctx = get_time_context(self._timezone)

        return f"""你是一个智能日程管理助手。你可以帮助用户：
- 创建和管理日程事件（会议、约会、提醒等）
- 创建和管理待办任务
- 查询日程和任务
- 规划时间安排

## 当前时间上下文
{time_ctx.to_prompt_string()}

## 工作原则
1. 理解用户的自然语言请求，选择合适的工具执行操作
2. 如果需要更多信息才能完成任务，主动询问用户
3. 执行操作后，用简洁友好的语言告诉用户结果
4. 时间相关的请求要结合当前时间上下文来理解
5. 如果用户说的日期已经过去，要提醒用户并确认是否是指未来的日期

## 工具使用
你可以使用以下工具来完成用户的请求。每个工具都有详细的使用说明。
在调用工具前，仔细阅读工具描述，确保参数正确。
"""

    def _add_assistant_message_with_tool_calls(self, response: LLMResponse) -> None:
        """
        添加包含工具调用的助手消息。

        Args:
            response: LLM 响应
        """
        tool_calls = []
        for tc in response.tool_calls:
            tool_calls.append({
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": tc.arguments
                    if isinstance(tc.arguments, str)
                    else json.dumps(tc.arguments, ensure_ascii=False),
                },
            })

        self._context.add_assistant_message(
            content=response.content, tool_calls=tool_calls
        )

    async def _execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        """
        执行工具调用。

        Args:
            tool_call: 工具调用

        Returns:
            工具执行结果
        """
        # 解析参数
        if isinstance(tool_call.arguments, str):
            try:
                kwargs = json.loads(tool_call.arguments)
            except json.JSONDecodeError:
                return ToolResult(
                    success=False,
                    error="INVALID_ARGUMENTS",
                    message=f"工具参数格式错误: {tool_call.arguments}",
                )
        else:
            kwargs = tool_call.arguments

        # 执行工具
        return await self._tool_registry.execute(tool_call.name, **kwargs)

    async def close(self) -> None:
        """关闭 Agent，释放资源"""
        await self._llm_client.close()

    async def __aenter__(self) -> "ScheduleAgent":
        """异步上下文管理器入口"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器退出"""
        await self.close()
