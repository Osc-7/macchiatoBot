"""
AgentKernel — 纯 IO 调度器（工具执行 + 生命周期管理）。

类比操作系统内核的 syscall 处理器：
- AgentCore 持有 LLMClient，自主完成多轮 LLM 推理（类比 CPU 自执行）
- 系统调用类型：
    ToolCallAction        — IO 中断（工具执行）
    ReturnAction          — 进程退出（本轮处理完成）
    ContextOverflowAction — 上下文溢出信号（暂停 → 压缩 → 恢复）[KNL-004 实现]
    CoreStatsAction       — kill 前资源上报（由 kill() 方法驱动）

设计优势：
1. AgentCore 多轮推理无需 Kernel 上下文切换，自旋效率更高
2. 工具调用仍由 Kernel 统一执行，安全策略集中可控（KNL-005 加权限校验）
3. 计费/监控状态在 Core 内积累，由 Kernel 在 kill 时通过 CoreStatsAction 收集
4. 多 Agent 协作天然等价于工具调用，无需额外设计
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from agent_core.interfaces import AgentHooks, AgentRunResult
from agent_core.kernel_interface import (
    ContextOverflowAction,
    CoreStatsAction,
    KernelAction,
    ReturnAction,
    ToolCallAction,
    ToolResultEvent,
)

if TYPE_CHECKING:
    from agent_core.agent.agent import ScheduleAgent
    # VersionedToolRegistry 由 system.tools 统一导出，避免直接依赖 agent_core.tools 装配细节
    from system.tools import VersionedToolRegistry

logger = logging.getLogger(__name__)


class AgentKernel:
    """
    Agent 系统内核：纯 IO 调度器。

    只持有 ToolRegistry（工具执行权）。
    通过 async generator 协议驱动 AgentCore 的 run_loop()，
    响应系统调用：ToolCallAction / ReturnAction / ContextOverflowAction。
    通过 kill() 方法收集 CoreStatsAction 完成进程回收前的资源上报。

    LLM 推理、Prompt 组装、logging、tracing 全部由 AgentCore 内部完成。

    用法::

        kernel = AgentKernel(tool_registry)
        result = await kernel.run(agent_core, turn_id=1, hooks=hooks)
        stats  = await kernel.kill(agent_core)   # 回收前调用
    """

    def __init__(
        self,
        tool_registry: "VersionedToolRegistry",
        # 以下参数保留仅为向后兼容，不再使用
        llm_client: Any = None,
        loader: Any = None,
        session_logger: Any = None,
    ) -> None:
        self._tools = tool_registry

    async def run(
        self,
        agent: "ScheduleAgent",
        turn_id: int = 0,
        hooks: Optional[AgentHooks] = None,
    ) -> AgentRunResult:
        """
        驱动 AgentCore 的 run_loop()。

        响应的系统调用：
        - ToolCallAction        → 执行工具，结果 asend 回 Core
        - ReturnAction          → 终止，返回 AgentRunResult
        - ContextOverflowAction → 占位处理（KNL-004 实现完整逻辑）
        """
        gen = agent.run_loop(turn_id=turn_id, hooks=hooks)
        action: KernelAction = await gen.__anext__()

        while True:
            if isinstance(action, ReturnAction):
                return AgentRunResult(
                    output_text=action.message,
                    attachments=action.attachments,
                )

            elif isinstance(action, ToolCallAction):
                # 内核态权限校验（双重防御：InternalLoader 已在用户态过滤，此处强制兜底）
                profile = getattr(agent, "_core_profile", None)
                if profile is not None and not profile.is_tool_allowed(action.tool_name):
                    from agent_core.tools.base import ToolResult as _ToolResult
                    denied_result = _ToolResult(
                        success=False,
                        data=None,
                        message=f"权限拒绝：工具 '{action.tool_name}' 不在该 Core 的权限范围内",
                        error="PERMISSION_DENIED",
                    )
                    action = await gen.asend(ToolResultEvent(
                        tool_call_id=action.tool_call_id,
                        result=denied_result,
                    ))
                    continue

                result = await self._tools.execute(
                    action.tool_name,
                    **self._parse_arguments(action.arguments),
                )
                action = await gen.asend(ToolResultEvent(
                    tool_call_id=action.tool_call_id,
                    result=result,
                ))

            elif isinstance(action, ContextOverflowAction):
                logger.info(
                    "AgentKernel: context overflow (tokens=%d, threshold=%d, session=%s), compressing…",
                    action.current_tokens,
                    action.threshold_tokens,
                    action.session_id,
                )
                from agent_core.kernel_interface import ContextCompressedEvent
                compressed_summary, messages_kept = await self._compress_context(agent)
                action = await gen.asend(ContextCompressedEvent(
                    compressed_summary=compressed_summary,
                    messages_kept=messages_kept,
                ))

            elif isinstance(action, CoreStatsAction):
                # run_loop 中不应出现 CoreStatsAction，仅 run_loop_kill 会产生
                logger.warning("AgentKernel.run: unexpected CoreStatsAction, stopping")
                return AgentRunResult(output_text="", metadata={"error": "unexpected_core_stats"})

            else:
                logger.warning("AgentKernel: unknown action type %r, stopping", type(action))
                return AgentRunResult(output_text="", metadata={"error": "unknown_action"})

    async def kill(self, agent: "ScheduleAgent") -> CoreStatsAction:
        """
        向 Core 发出 Kill 指令，等待 CoreStatsAction 资源上报后返回。

        由 CorePool.evict() 调用，在 close() 之前执行。
        CorePool 拿到 CoreStatsAction 后再调用 SessionSummarizer.summarize_and_persist()。

        若 Core 不支持 run_loop_kill()（旧版兼容），返回空 CoreStatsAction。
        """
        run_loop_kill = getattr(agent, "run_loop_kill", None)
        if not callable(run_loop_kill):
            logger.warning("AgentKernel.kill: agent does not support run_loop_kill, skipping")
            return CoreStatsAction(session_id=getattr(agent, "_session_id", ""))

        try:
            gen = run_loop_kill()
            action = await gen.__anext__()
            if isinstance(action, CoreStatsAction):
                logger.debug(
                    "AgentKernel.kill: collected CoreStats session=%s turns=%d tokens=%d",
                    action.session_id,
                    action.turn_count,
                    action.token_usage.get("total_tokens", 0),
                )
                return action
            logger.warning("AgentKernel.kill: unexpected action type %r", type(action))
        except StopAsyncIteration:
            pass
        except Exception as exc:
            logger.warning("AgentKernel.kill: run_loop_kill failed: %s", exc)

        return CoreStatsAction(session_id=getattr(agent, "_session_id", ""))

    async def _compress_context(
        self,
        agent: "ScheduleAgent",
        keep_recent_turns: int = 6,
    ) -> tuple[str, int]:
        """
        压缩 agent 的对话上下文。

        策略：
        1. 取出全部 messages，保留最近 keep_recent_turns 轮（含 tool_result）
        2. 将旧部分通过 summary_llm_client 生成一段摘要文本
        3. 将旧消息从 context 中截断（只保留 system-adjacent 部分 + 新消息）
        4. 返回 (摘要文本, 保留的完整消息数)

        若 LLM 摘要失败，退化为不摘要的纯截断（保证 Kernel 不因摘要失败而卡住）。
        """
        ctx = getattr(agent, "_context", None)
        if ctx is None:
            return "", 0

        messages = ctx.get_messages()
        if len(messages) <= keep_recent_turns * 2:
            return "", len(messages)

        # 保留最近 keep_recent_turns * 2 条（每轮大约 user+assistant 两条）
        split_idx = max(0, len(messages) - keep_recent_turns * 2)
        old_messages = messages[:split_idx]
        new_messages = messages[split_idx:]

        summary_text = await self._summarize_messages(agent, old_messages)

        # 截断 context：只保留新消息
        ctx.messages = list(new_messages)

        logger.info(
            "AgentKernel: compressed %d old messages → summary (%d chars), kept %d messages",
            len(old_messages),
            len(summary_text),
            len(new_messages),
        )
        return summary_text, len(new_messages)

    @staticmethod
    async def _summarize_messages(
        agent: "ScheduleAgent",
        messages: list,
    ) -> str:
        """用 summary_llm_client 为旧消息生成摘要，失败时返回空字符串。"""
        llm = getattr(agent, "_summary_llm_client", None)
        if llm is None or not messages:
            return ""

        dialogue_lines = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if isinstance(content, str) and content.strip() and role in ("user", "assistant"):
                dialogue_lines.append(f"[{role}]: {content[:400]}")

        if not dialogue_lines:
            return ""

        prompt = (
            "请用 3-5 句话概括以下对话的核心内容、关键决定和重要信息，供后续对话参考。\n\n"
            + "\n".join(dialogue_lines[-20:])
        )
        try:
            resp = await llm.chat(
                system_message="你是一个对话摘要助手，输出简洁的中文摘要。",
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content.strip() if resp and resp.content else ""
        except Exception as exc:
            logger.warning("AgentKernel._summarize_messages: LLM call failed: %s", exc)
            return ""

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
