"""
AgentKernel — IO 控制器，驱动 AgentCore 状态机的执行循环。

类比操作系统内核：
- 持有所有 IO 能力（LLMClient、ToolRegistry）
- 通过 async generator 协议（yield/asend）驱动 AgentCore
- AgentCore 只声明意图（KernelAction），Kernel 执行实际 IO
- 记录 trace events、session log、token 统计

设计原则：
- AgentCore 对 LLM 和 Tool 的访问完全受 Kernel 中介
- Kernel 是唯一掌握"全局调度策略"的地方（限流、日志、计费）
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

from agent.core.interfaces import AgentHooks, AgentRunInput, AgentRunResult
from agent.core.llm import LLMResponse, ToolCall
from agent.core.tools import ToolResult

from .action import (
    KernelAction,
    LLMRequestAction,
    LLMResponseEvent,
    ReturnAction,
    ToolCallAction,
    ToolResultEvent,
)
from .loader import InternalLoader

if TYPE_CHECKING:
    from agent.core.agent.agent import ScheduleAgent
    from agent.core.llm import LLMClient
    from agent.core.tools import VersionedToolRegistry
    from agent.utils.session_logger import SessionLogger

logger = logging.getLogger(__name__)


class AgentKernel:
    """
    Agent 系统内核。

    持有 LLMClient + ToolRegistry + InternalLoader，
    通过 async generator 协议驱动 AgentCore 的 run_loop()。

    用法::

        kernel = AgentKernel(llm_client, tool_registry, loader, session_logger)
        result = await kernel.run(agent_core, turn_id=1, hooks=hooks)
    """

    def __init__(
        self,
        llm_client: "LLMClient",
        tool_registry: "VersionedToolRegistry",
        loader: Optional[InternalLoader] = None,
        session_logger: Optional["SessionLogger"] = None,
    ) -> None:
        self._llm = llm_client
        self._tools = tool_registry
        self._loader = loader or InternalLoader()
        self._session_logger = session_logger

    async def run(
        self,
        agent: "ScheduleAgent",
        turn_id: int = 0,
        hooks: Optional[AgentHooks] = None,
    ) -> AgentRunResult:
        """
        驱动 AgentCore 的 run_loop() 直到返回 ReturnAction。

        执行流程：
        1. 启动 agent.run_loop() async generator
        2. 循环接收 KernelAction：
           - LLMRequestAction → InternalLoader.assemble() → LLMClient.chat_with_tools()
           - ToolCallAction → ToolRegistry.execute()
           - ReturnAction → 终止，返回 AgentRunResult
        3. 每步通过 asend(KernelEvent) 将结果回传给 AgentCore
        """
        gen = agent.run_loop(turn_id=turn_id, hooks=hooks)

        # 启动 generator（到第一个 yield）
        action: KernelAction = await gen.__anext__()
        iteration = 0

        while True:
            iteration += 1

            if isinstance(action, ReturnAction):
                return AgentRunResult(
                    output_text=action.message,
                    attachments=action.attachments,
                )

            elif isinstance(action, LLMRequestAction):
                # 组装 Payload（Prompt + Context + Tools）
                payload = self._loader.assemble(agent)

                # SessionLogger
                if self._session_logger:
                    self._session_logger.on_llm_request(
                        turn_id=turn_id,
                        iteration=iteration,
                        message_count=len(payload.messages),
                        tool_count=len(payload.tools),
                        system_prompt_len=len(payload.system),
                        system_prompt=payload.system if self._session_logger.enable_detailed_log else None,
                        messages=payload.messages if self._session_logger.enable_detailed_log else None,
                    )

                # trace event
                await self._emit_trace(
                    hooks,
                    {
                        "type": "llm_request",
                        "turn_id": turn_id,
                        "iteration": iteration,
                        "tool_count": len(payload.tools),
                    },
                )

                # 调用 LLM（IO）
                response = await self._llm.chat_with_tools(
                    system_message=payload.system,
                    messages=payload.messages,
                    tools=payload.tools,
                    tool_choice="auto",
                    on_content_delta=hooks.on_assistant_delta if hooks else None,
                    on_reasoning_delta=hooks.on_reasoning_delta if hooks else None,
                )

                if self._session_logger:
                    self._session_logger.on_llm_response(turn_id, iteration, response)

                # 将 LLMResponse 传回 AgentCore
                action = await gen.asend(LLMResponseEvent(response=response))

            elif isinstance(action, ToolCallAction):
                # trace event
                await self._emit_trace(
                    hooks,
                    {
                        "type": "tool_call",
                        "turn_id": turn_id,
                        "iteration": iteration,
                        "tool_call_id": action.tool_call_id,
                        "name": action.tool_name,
                        "arguments": action.arguments,
                    },
                )

                if self._session_logger:
                    self._session_logger.on_tool_call(
                        turn_id,
                        iteration,
                        ToolCall(
                            id=action.tool_call_id,
                            name=action.tool_name,
                            arguments=action.arguments or {},
                        ),
                    )

                # 执行工具（IO）
                t0 = time.perf_counter()
                result = await self._tools.execute(
                    action.tool_name,
                    **self._parse_arguments(action.arguments),
                )
                duration_ms = int((time.perf_counter() - t0) * 1000)

                await self._emit_trace(
                    hooks,
                    {
                        "type": "tool_result",
                        "turn_id": turn_id,
                        "iteration": iteration,
                        "tool_call_id": action.tool_call_id,
                        "name": action.tool_name,
                        "success": result.success,
                        "message": result.message,
                        "duration_ms": duration_ms,
                        "error": result.error,
                    },
                )

                if self._session_logger:
                    self._session_logger.on_tool_result(
                        turn_id, iteration, action.tool_call_id, result, duration_ms
                    )

                # 将 ToolResult 传回 AgentCore
                action = await gen.asend(ToolResultEvent(
                    tool_call_id=action.tool_call_id,
                    result=result,
                ))

            else:
                logger.warning("AgentKernel: unknown action type %r, stopping", type(action))
                return AgentRunResult(output_text="", metadata={"error": "unknown_action"})

    @staticmethod
    def _parse_arguments(arguments: Any) -> Dict[str, Any]:
        """将工具参数统一解析为 dict。"""
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
                return parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, ValueError):
                return {}
        return {}

    @staticmethod
    async def _emit_trace(
        hooks: Optional[AgentHooks],
        event: Dict[str, Any],
    ) -> None:
        """安全地触发 on_trace_event 回调（支持 sync/async）。"""
        if hooks is None or hooks.on_trace_event is None:
            return
        maybe = hooks.on_trace_event(event)
        if inspect.isawaitable(maybe):
            await maybe
