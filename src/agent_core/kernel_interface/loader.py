"""
InternalLoader — 在每次 LLM 调用前，动态组装完整的请求 Payload。

对应架构图 Bottom Left 中 Loader 组件：
    Prompt | Context | Messages | Tool Result

职责边界：
- 只做数据组装，不做任何网络 IO
- 从 AgentCore 的公开属性读取状态，避免与 AgentCore 强耦合
- 由 AgentCore.run_loop() 在每次直接调用 LLM 前使用
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from agent_core.agent.agent import AgentCore


def _strip_orphan_tool_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    移除孤立的 tool 消息。
    API 要求：tool 消息必须紧接在 assistant+tool_calls 之后。
    """
    out: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "")
        if role == "tool":
            prev = out[-1] if out else {}
            if prev.get("role") == "assistant" and prev.get("tool_calls"):
                out.append(m)
            # 否则跳过该 tool（孤立）
        else:
            out.append(m)
    return out


@dataclass
class LLMPayload:
    """组装好的 LLM 请求 Payload，供 LLMClient 直接使用。"""

    system: str
    messages: List[Dict[str, Any]]
    tools: List[Dict[str, Any]]


class InternalLoader:
    """
    内部 Loader：从 AgentCore 状态组装 LLM 请求 Payload。

    类比 OS Kernel 在发起系统调用前的参数准备阶段，负责将：
    - Prompt（系统提示、记忆上下文、摘要）
    - Context（当前对话消息历史）
    - Tools（当前可见工具快照）
    打包成一次完整的 LLM 请求。
    """

    def assemble(self, agent: "AgentCore") -> LLMPayload:
        """
        从 AgentCore 状态动态组装 LLMPayload。

        每次 LLM 调用前调用，确保 Prompt/Context/Tools 都是最新状态。
        若 agent._core_profile 不为 None，则对工具列表进行用户态过滤（双重防御的第一层）。
        """
        system_prompt = agent._build_system_prompt()
        messages = list(agent._context.get_messages())
        messages = _strip_orphan_tool_messages(messages)

        # 注入待处理的多模态内容（图片/视频）
        if agent._pending_multimodal_items:
            messages = agent._append_pending_multimodal_messages(messages)
            agent._pending_multimodal_items.clear()

        # 工具快照：统一由工作集决定首轮暴露 + 后续 search_tools 扩展的可见集
        agent._last_snapshot = agent._working_set.build_snapshot(agent._tool_registry)
        tools = agent._last_snapshot.openai_tools
        visible_names: set = set(agent._last_snapshot.tool_names)

        # 用户态权限过滤：根据 CoreProfile 限制 LLM 可见的工具集
        profile = getattr(agent, "_core_profile", None)
        if profile is not None:
            tools = [
                t
                for t in tools
                if profile.is_tool_allowed(t.get("function", {}).get("name", ""))
            ]
            visible_names = {
                name for name in visible_names if profile.is_tool_allowed(name)
            }

        agent._current_visible_tools = visible_names
        return LLMPayload(system=system_prompt, messages=messages, tools=tools)
