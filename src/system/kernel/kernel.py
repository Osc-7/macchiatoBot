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
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from agent_core.interfaces import AgentHooks, AgentRunResult
from agent_core.agent.tool_path_resolution import (
    apply_workspace_path_resolution_to_tool_args,
)
from agent_core.kernel_interface import (
    ContextOverflowAction,
    CoreStatsAction,
    KernelAction,
    ReturnAction,
    ToolCallAction,
    ToolResultEvent,
)

if TYPE_CHECKING:
    from agent_core.agent.agent import AgentCore

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
        agent: "AgentCore",
        turn_id: int = 0,
        hooks: Optional[AgentHooks] = None,
        on_signal: Optional[Callable[[], None]] = None,
        *,
        channel_metadata: Optional[Dict[str, Any]] = None,
    ) -> AgentRunResult:
        """
        驱动 AgentCore 的 run_loop()。

        channel_metadata: 来自 KernelRequest.metadata 的渠道字段（如 feishu_chat_id），
        会并入工具 __execution_context__，供 request_permission、ask_user 等推送到对应前端。

        响应的系统调用：
        - ToolCallAction        → 执行工具，结果 asend 回 Core
        - ReturnAction          → 终止，返回 AgentRunResult
        - ContextOverflowAction → 占位处理（KNL-004 实现完整逻辑）

        on_signal: 每次收到 ReturnAction 或 ToolCallAction 时调用，用于刷新 TTL 等。
        """
        gen = agent.run_loop(turn_id=turn_id, hooks=hooks)
        action: KernelAction = await gen.__anext__()
        tool_names_called: set[str] = set()

        def _maybe_touch() -> None:
            if on_signal:
                try:
                    on_signal()
                except Exception as exc:
                    logger.debug("AgentKernel.run: on_signal callback failed: %s", exc)

        while True:
            if isinstance(action, ReturnAction):
                _maybe_touch()
                meta: Dict[str, Any] = {}
                if tool_names_called:
                    meta["_tool_names_called"] = sorted(tool_names_called)
                return AgentRunResult(
                    output_text=action.message,
                    attachments=action.attachments,
                    metadata=meta,
                )

            elif isinstance(action, ToolCallAction):
                _maybe_touch()
                visible_tools = set(getattr(agent, "_current_visible_tools", set()) or set())
                if visible_tools and action.tool_name not in visible_tools:
                    from agent_core.tools.base import ToolResult as _ToolResult

                    denied_result = _ToolResult(
                        success=False,
                        data=None,
                        message=f"工具 '{action.tool_name}' 当前不在可见工作集中",
                        error="TOOL_NOT_VISIBLE",
                    )
                    action = await gen.asend(
                        ToolResultEvent(
                            tool_call_id=action.tool_call_id,
                            result=denied_result,
                        )
                    )
                    continue
                # 内核态权限校验（双重防御：InternalLoader 已在用户态过滤，此处强制兜底）
                profile = getattr(agent, "_core_profile", None)
                if profile is not None and not profile.is_tool_allowed(
                    action.tool_name
                ):
                    from agent_core.tools.base import ToolResult as _ToolResult

                    denied_result = _ToolResult(
                        success=False,
                        data=None,
                        message=f"权限拒绝：工具 '{action.tool_name}' 不在该 Core 的权限范围内",
                        error="PERMISSION_DENIED",
                    )
                    action = await gen.asend(
                        ToolResultEvent(
                            tool_call_id=action.tool_call_id,
                            result=denied_result,
                        )
                    )
                    continue

                # 优先使用 agent 自身的 per-session registry（已过 CoreProfile 过滤），
                # 避免 call_tool 通过全局 registry 绕过权限限制。
                agent_registry = getattr(agent, "_tool_registry", None) or self._tools
                parsed_args, parse_err = self._parse_arguments(action.arguments)
                if parse_err is not None:
                    from agent_core.tools.base import ToolResult as _ToolResult

                    result = _ToolResult(
                        success=False,
                        error="INVALID_ARGUMENTS",
                        message=parse_err,
                        data=None,
                    )
                else:
                    # 注入执行上下文：让 bash / file_tools 等能感知当前 CoreProfile。
                    profile_mode = getattr(profile, "mode", "full") if profile is not None else "full"
                    _ctx = {
                        "profile_mode": profile_mode,
                        "tool_template": getattr(profile, "tool_template", "default")
                        if profile is not None
                        else "default",
                        "allow_dangerous_commands": getattr(
                            profile, "allow_dangerous_commands", False
                        )
                        if profile is not None
                        else False,
                        "bash_workspace_admin": bool(
                            getattr(profile, "bash_workspace_admin", False)
                        )
                        if profile is not None
                        else False,
                        "source": getattr(agent, "_source", ""),
                        "user_id": getattr(agent, "_user_id", ""),
                        "session_id": getattr(agent, "_session_id", ""),
                    }
                    _jmo = getattr(agent, "_job_memory_owner", None)
                    if _jmo:
                        _ctx["memory_owner"] = _jmo
                    if channel_metadata:
                        for _k in ("feishu_chat_id", "feishu_open_id"):
                            _v = channel_metadata.get(_k)
                            if _v is not None and str(_v).strip():
                                _ctx[_k] = str(_v).strip()
                    parsed_args["__execution_context__"] = _ctx
                    cfg = getattr(agent, "_config", None)
                    if cfg is not None:
                        parsed_args = apply_workspace_path_resolution_to_tool_args(
                            action.tool_name, parsed_args, cfg
                        )
                    result = await agent_registry.execute(
                        action.tool_name, **parsed_args
                    )
                    tool_names_called.add(action.tool_name)
                    delegated = result.metadata.get("_delegated_tool_name")
                    if isinstance(delegated, str) and delegated:
                        tool_names_called.add(delegated)
                action = await gen.asend(
                    ToolResultEvent(
                        tool_call_id=action.tool_call_id,
                        result=result,
                    )
                )

            elif isinstance(action, ContextOverflowAction):
                logger.info(
                    "AgentKernel: context overflow (tokens=%d, threshold=%d, session=%s), compressing…",
                    action.current_tokens,
                    action.threshold_tokens,
                    action.session_id,
                )
                from agent_core.kernel_interface import ContextCompressedEvent

                compressed_summary, messages_kept = await self._compress_context(agent)
                action = await gen.asend(
                    ContextCompressedEvent(
                        compressed_summary=compressed_summary,
                        messages_kept=messages_kept,
                    )
                )

            elif isinstance(action, CoreStatsAction):
                # run_loop 中不应出现 CoreStatsAction，仅 run_loop_kill 会产生
                logger.warning("AgentKernel.run: unexpected CoreStatsAction, stopping")
                return AgentRunResult(
                    output_text="", metadata={"error": "unexpected_core_stats"}
                )

            else:
                logger.warning(
                    "AgentKernel: unknown action type %r, stopping", type(action)
                )
                return AgentRunResult(
                    output_text="", metadata={"error": "unknown_action"}
                )

    async def kill(self, agent: "AgentCore") -> CoreStatsAction:
        """
        向 Core 发出 Kill 指令，等待 CoreStatsAction 资源上报后返回。

        由 CorePool.evict() 调用，在 close() 之前执行。
        CorePool 拿到 CoreStatsAction 后再调用 SessionSummarizer.summarize_and_persist()。

        若 Core 不支持 run_loop_kill()（旧版兼容），返回空 CoreStatsAction。
        """
        run_loop_kill = getattr(agent, "run_loop_kill", None)
        if not callable(run_loop_kill):
            logger.warning(
                "AgentKernel.kill: agent does not support run_loop_kill, skipping"
            )
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

    _SUMMARY_USER_PREFIX = "[会话进行中摘要]"

    async def _compress_context(
        self,
        agent: "AgentCore",
        keep_recent_turns: Optional[int] = None,
    ) -> tuple[str, int]:
        """
        压缩对话上下文。

        1. 按 user 轮切分，保留最近 keep_recent_turns 轮；其余为待折叠段。
        2. 将待折叠段（含 assistant / tool / tool_result）原样交给 summary LLM，最后一条 user 为「请总结」。
        3. 若得到摘要：清空已折叠段，在 messages 里只留一条摘要 user，再拼上保留段；并写入 running_summary（checkpoint / 会话结束总结等元数据，不再单独注入主 system）。
        4. 若摘要失败：仅截断为保留段，不插入摘要消息。

        返回 (摘要文本, 压缩后 messages 条数)。
        """
        if keep_recent_turns is None:
            profile = getattr(agent, "_core_profile", None)
            keep_recent_turns = int(
                getattr(profile, "compress_keep_recent_turns", None)
                or getattr(
                    getattr(agent, "_config", None),
                    "compress_keep_recent_turns",
                    6,
                )
                or 6
            )
        ctx = getattr(agent, "_context", None)
        if ctx is None:
            return "", 0

        messages = ctx.get_messages()
        if len(messages) <= keep_recent_turns * 2:
            return "", len(messages)

        # 以 user 消息为轮次边界，保留最近 N 个完整轮次（可包含 assistant/tool 链）
        user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        if len(user_indices) <= keep_recent_turns:
            return "", len(messages)
        split_idx = user_indices[-keep_recent_turns]
        old_messages = messages[:split_idx]
        new_messages = messages[split_idx:]

        wm = getattr(agent, "_working_memory", None)
        fold_before = int(getattr(wm, "compression_round", 0) or 0) if wm else 0
        summary_text = await self._summarize_messages(agent, old_messages)

        if summary_text.strip():
            summary_msg = {
                "role": "user",
                "content": (
                    f"{self._SUMMARY_USER_PREFIX}\n{summary_text.strip()}"
                ),
            }
            ctx.messages = [summary_msg] + list(new_messages)
            if wm is not None:
                wm.running_summary = summary_text.strip()
                wm.compression_round = fold_before + 1
        else:
            ctx.messages = list(new_messages)

        kept = len(ctx.messages)
        logger.info(
            "AgentKernel: compressed %d old messages → summary (%d chars), kept %d messages",
            len(old_messages),
            len(summary_text),
            kept,
        )
        return summary_text, kept

    _COMPRESS_SYSTEM_APPEND = """\
# 上下文压缩

当前不是在正常回复用户，而是在上下文触顶后，对 **messages 中的对话** 做折叠摘要。
对话含 user / assistant / tool 调用与 tool 结果；最后一条 user 是总结指令。
请输出一段可延续的摘要正文：用户目标、关键事实、工具结果中的数据、待办与未决问题；语言与对话一致。"""

    _SUMMARIZE_USER_APPEND = "请总结以上对话。"

    @staticmethod
    def _system_message_for_compression(agent: "AgentCore") -> str:
        """与主对话同一套 build_agent_system_prompt，再追加压缩任务段。"""
        builder = getattr(agent, "_build_system_prompt", None)
        base = ""
        if callable(builder):
            try:
                base = (builder() or "").strip()
            except Exception as exc:
                logger.warning(
                    "AgentKernel: _build_system_prompt failed for compression: %s", exc
                )
        append = AgentKernel._COMPRESS_SYSTEM_APPEND.strip()
        if base:
            return f"{base}\n\n{append}"
        return append

    @staticmethod
    async def _summarize_messages(
        agent: "AgentCore",
        messages: list,
    ) -> str:
        """待折叠段浅拷贝进 API messages（避免客户端就地改 dict 影响尚未替换的上下文），末尾追加「请总结」。"""
        llm = getattr(agent, "_summary_llm_client", None)
        if llm is None or not messages:
            return ""

        # 浅拷贝即可：总结完成后原段会从 context 丢弃；此处仅防 LLM 客户端改写 message dict
        chat_messages: List[Dict[str, Any]] = [dict(m) for m in messages]
        chat_messages.append(
            {"role": "user", "content": AgentKernel._SUMMARIZE_USER_APPEND}
        )

        system_message = AgentKernel._system_message_for_compression(agent)

        try:
            resp = await llm.chat(
                system_message=system_message,
                messages=chat_messages,
            )
            return (resp.content or "").strip() if resp else ""
        except Exception as exc:
            logger.warning("AgentKernel._summarize_messages: LLM call failed: %s", exc)
            return ""

    @staticmethod
    def _parse_arguments(arguments: Any) -> tuple[Dict[str, Any], Optional[str]]:
        """将工具参数统一解析为 dict。返回 (parsed_dict, None) 或 ({}, error_message)。"""
        if isinstance(arguments, dict):
            return arguments, None
        if isinstance(arguments, str):
            if not arguments.strip():
                return {}, "工具参数为空字符串"
            try:
                parsed = json.loads(arguments)
                if not isinstance(parsed, dict):
                    return {}, "工具参数必须是 JSON 对象"
                return parsed, None
            except (json.JSONDecodeError, ValueError):
                preview = arguments[:500] + ("...(截断)" if len(arguments) > 500 else "")
                return {}, f"工具参数 JSON 解析失败（可能为流式输出截断）: {preview}"
        return {}, "工具参数类型无效"
