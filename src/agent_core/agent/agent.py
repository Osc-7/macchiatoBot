"""
主 Agent 实现

实现基于工具驱动的 Agent 循环，支持多轮对话和工具调用。
集成四层记忆架构：工作记忆、短期记忆、长期记忆、内容记忆。
"""

import asyncio
import inspect
import json
import os
import sys
import time
from datetime import datetime
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from agent_core.config import Config, MemoryConfig, MCPServerConfig, get_config
from agent_core.context import ConversationContext
from agent_core.utils.billing import compute_cost_from_calls
from agent_core.mcp import MCPClientManager
from agent_core.orchestrator import ToolSnapshot, ToolWorkingSetManager
from agent_core.llm import (
    LLMClient,
    LLMResponse,
    ToolCall,
    TokenUsage,
    get_context_window_tokens_for_model,
)
from agent_core.utils.media import resolve_media_to_content_item
from agent_core.tools import (
    BaseTool,
    CallToolTool,
    SearchToolsTool,
    ToolResult,
    VersionedToolRegistry,
    WebExtractorTool,
    WebSearchTool,
)
from agent_core.tools.chat_history_tools import (
    ChatSearchTool,
    ChatContextTool,
    ChatScrollTool,
)
from agent_core.memory import (
    WorkingMemory,
    LongTermMemory,
    ContentMemory,
    RecallPolicy,
    RecallResult,
    SessionSummary,
    ChatHistoryDB,
)
from .media_helpers import (
    append_pending_multimodal_messages,
    collect_outgoing_attachment,
    queue_media_for_next_call,
)
from .memory_paths import new_session_id, resolve_memory_owner_paths
from .prompt_builder import build_agent_system_prompt

if TYPE_CHECKING:
    from agent_core.interfaces import AgentHooks, AgentRunResult
    from agent_core.utils.session_logger import SessionLogger


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
        session_logger: Optional["SessionLogger"] = None,
        user_id: str = "root",
        source: str = "cli",
        defer_mcp_connect: bool = False,
    ):
        """
        初始化 Agent。

        Args:
            config: 配置对象，如果为 None 则使用全局配置
            tools: 工具列表，如果为 None 则使用空注册表
            max_iterations: 最大工具调用迭代次数
            timezone: 时区
            session_logger: 会话日志记录器，用于记录完整 session 日志
            user_id: 记忆命名空间用户 ID（同一 user_id 可跨终端共享记忆）
            source: 来源命名空间（如 cli/qq/whatsapp）
            defer_mcp_connect: 为 True 时 __aenter__ 不连接 MCP，需稍后调用 ensure_mcp_connected()（用于 daemon 先完成启动再连 MCP）
        """
        self._config = config or get_config()
        self._user_id = user_id.strip() or "root"
        self._source = source.strip() or "cli"
        self._llm_client = LLMClient(self._config)
        summary_model = getattr(self._config.llm, "summary_model", None)
        self._summary_llm_client = (
            LLMClient(self._config, model_override=summary_model)
            if summary_model
            else self._llm_client
        )
        self._tool_registry = VersionedToolRegistry()
        self._context = ConversationContext()
        self._max_iterations = max_iterations
        self._timezone = timezone
        self._session_logger = session_logger
        # 按 source 覆盖 tool_mode；full 视为 kernel 向后兼容
        agent_cfg = self._config.agent
        raw_mode = (agent_cfg.source_overrides or {}).get(
            self._source, agent_cfg.tool_mode or "kernel"
        ) or "kernel"
        if (raw_mode or "").lower() == "full":
            raw_mode = "kernel"
        self._effective_tool_mode = (raw_mode or "kernel").lower()
        self._kernel_enabled = self._effective_tool_mode == "kernel"
        pinned_tools = list(self._config.agent.pinned_tools or [])
        for core_name in ["search_tools", "call_tool"]:
            if core_name not in pinned_tools:
                pinned_tools.append(core_name)
        self._working_set = ToolWorkingSetManager(
            pinned_tools=pinned_tools,
            working_set_size=self._config.agent.working_set_size,
        )
        self._last_snapshot = ToolSnapshot(version=-1, tool_names=[], openai_tools=[])
        self._current_visible_tools: set[str] = set()
        self._pending_multimodal_items: List[Dict[str, Any]] = []
        # 本轮回复要附带发给用户的图片等附件（由 attach_image_to_reply 等工具登记）
        self._outgoing_attachments: List[Dict[str, Any]] = []
        # 本会话 token 用量累计
        self._token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "call_count": 0}
        # 每次调用的 (prompt_tokens, completion_tokens)，用于阶梯计费
        self._usage_calls: List[Tuple[int, int]] = []
        # 上一轮 LLM 的 prompt_tokens，供工作记忆阈值判断
        self._last_prompt_tokens: Optional[int] = None
        # 当前轮次（每次 process_input 递增）
        self._current_turn_id = 0
        # 会话起始时间
        self._session_start_time = datetime.now(dt_timezone.utc).isoformat()
        # 会话 ID（用于 ChatHistoryDB 写入分组）
        self._session_id = new_session_id()
        # ChatHistoryDB 最后同步到的消息 ID（用于跨终端增量同步）
        self._last_history_id: int = 0
        # CoreProfile — Kernel 注入的权限配置；None 表示无限制（向后兼容）
        self._core_profile: Optional[Any] = None

        # 四层记忆系统
        mem_cfg: MemoryConfig = self._config.memory
        self._memory_enabled = mem_cfg.enabled
        source_paths = resolve_memory_owner_paths(
            mem_cfg, self._user_id, config=self._config, source=self._source
        )

        self._working_memory = WorkingMemory(
            context=self._context,
            max_tokens=mem_cfg.max_working_tokens,
            threshold=mem_cfg.working_summary_threshold,
            keep_recent=mem_cfg.working_keep_recent,
            hard_threshold_ratio=mem_cfg.working_summary_hard_ratio,
        )
        self._long_term_memory = LongTermMemory(
            storage_dir=source_paths["long_term_dir"],
            memory_md_path=source_paths["memory_md_path"],
            qmd_enabled=mem_cfg.qmd_enabled,
            qmd_command=mem_cfg.qmd_command,
        )
        self._content_memory = ContentMemory(
            content_dir=source_paths["content_dir"],
            qmd_enabled=mem_cfg.qmd_enabled,
            qmd_command=mem_cfg.qmd_command,
        )
        self._recall_policy = RecallPolicy(
            force_recall=mem_cfg.force_recall,
            top_n=mem_cfg.recall_top_n,
            score_threshold=mem_cfg.recall_score_threshold,
        )
        self._chat_history_db = ChatHistoryDB(
            source_paths["chat_history_db_path"],
            default_source=None,
        )

        # 注册工具
        if tools:
            for tool in tools:
                self._tool_registry.register(tool)

        # 注册对话历史检索工具
        if self._memory_enabled:
            for chat_tool in [
                ChatSearchTool(self._chat_history_db),
                ChatContextTool(self._chat_history_db),
                ChatScrollTool(self._chat_history_db),
            ]:
                if not self._tool_registry.has(chat_tool.name):
                    self._tool_registry.register(chat_tool)

        # kernel 模式下补充核心工具
        if self._kernel_enabled:
            if not self._tool_registry.has("search_tools"):
                self._tool_registry.register(
                    SearchToolsTool(
                        registry=self._tool_registry,
                        working_set=self._working_set,
                    )
                )
            if not self._tool_registry.has("call_tool"):
                self._tool_registry.register(
                    CallToolTool(
                        registry=self._tool_registry,
                    )
                )

        # MCP 客户端（在 __aenter__ 中连接，或 defer 时由 ensure_mcp_connected 连接）
        self._mcp_manager: Optional[MCPClientManager] = None
        self._mcp_connected = False
        self._defer_mcp_connect = defer_mcp_connect

        # 联网工具（基于 Tavily MCP）
        if self._config.mcp.enabled:
            if not self._tool_registry.has("web_search"):
                self._tool_registry.register(WebSearchTool(registry=self._tool_registry))
            if not self._tool_registry.has("extract_web_content"):
                self._tool_registry.register(WebExtractorTool(registry=self._tool_registry))

    @property
    def config(self) -> Config:
        """获取当前配置"""
        return self._config

    @property
    def tool_registry(self) -> VersionedToolRegistry:
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

    def delete_session_history(self, session_id: Optional[str] = None) -> int:
        """
        删除指定 session 的对话历史。

        仅删除 ChatHistoryDB 中该 session + source 的消息记录，不影响长期记忆。
        默认使用当前 Agent 的 session_id。
        """
        sid = (session_id or self._session_id or "").strip()
        if not sid or not self._memory_enabled:
            return 0
        return self._chat_history_db.delete_session_messages(sid, source=self._source)

    def get_token_usage(self) -> dict:
        """
        获取本会话累计的 token 用量。

        Returns:
            包含 prompt_tokens, completion_tokens, total_tokens, call_count, cost_yuan 等字段的字典
        """
        out: dict[str, int | float] = dict(self._token_usage)

        # 上下文窗口（context window）相关信息
        try:
            model_name = self._llm_client.model
        except Exception:
            model_name = self._config.llm.model

        max_ctx_tokens = get_context_window_tokens_for_model(model_name)
        if max_ctx_tokens and max_ctx_tokens > 0:
            # 当前上下文 token 数：
            # 优先使用上一轮真实的 prompt_tokens（包含 system + messages），
            # 若不存在则回退到根据当前消息估算。
            current_ctx_tokens: int
            if self._last_prompt_tokens is not None and self._last_prompt_tokens > 0:
                current_ctx_tokens = int(self._last_prompt_tokens)
            else:
                # 估算当前上下文长度（仅基于 messages），这里不额外估算 system，
                # 只作为无 usage 时的近似值。
                current_ctx_tokens = self._working_memory.get_current_tokens()

            remaining_ctx_tokens = max(max_ctx_tokens - current_ctx_tokens, 0)
            out["context_window_max_tokens"] = max_ctx_tokens
            out["context_window_current_tokens"] = current_ctx_tokens
            out["context_window_remaining_tokens"] = remaining_ctx_tokens

        cost = compute_cost_from_calls(
            self._usage_calls,
            self._config.llm.model,
        )
        if cost is not None:
            out["cost_yuan"] = cost
        return out

    def get_turn_count(self) -> int:
        """获取本会话已处理的用户轮次数量"""
        return self._current_turn_id

    def reset_token_usage(self) -> None:
        """重置本会话的 token 用量统计"""
        self._token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "call_count": 0}
        self._usage_calls.clear()

    async def process_input(
        self,
        user_input: str,
        content_items: Optional[List[Dict[str, Any]]] = None,
        on_stream_delta: Optional[Callable[[str], Any]] = None,
        on_reasoning_delta: Optional[Callable[[str], Any]] = None,
        on_trace_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> str:
        """
        向后兼容入口，内部委托给 AgentKernel。

        这是 Agent 的公开主入口点，调用方无需感知 Kernel 架构，
        行为与重构前完全一致。

        Args:
            user_input: 用户输入
            content_items: 前端解析的多模态内容（image_url/video_url），与 user_input 一并注入本轮 LLM
            on_stream_delta: 流式文本增量回调（仅文本内容）
            on_reasoning_delta: 思维链增量回调（reasoning_content）
            on_trace_event: 轨迹事件回调（工具调用、结果、轮次）

        Returns:
            Agent 的响应文本
        """
        from agent_core.interfaces import AgentHooks
        from system.kernel import AgentKernel

        await self._sync_external_session_updates()
        self._current_turn_id += 1
        turn_id = self._current_turn_id

        # 记忆检索 enrich
        if self._memory_enabled and self._recall_policy.should_recall(user_input):
            recall_result = await asyncio.to_thread(
                self._recall_policy.recall,
                query=user_input,
                long_term_memory=self._long_term_memory,
                content_memory=self._content_memory,
            )
            self._last_recall_result = recall_result
        else:
            self._last_recall_result = RecallResult()

        # 准备上下文（图片等多模态内容合并进用户消息，当轮首条 LLM 即可看到）
        self._context.add_user_message(user_input, media_items=content_items or None)
        self._outgoing_attachments.clear()
        if self._session_logger:
            self._session_logger.on_user_message(turn_id, user_input)
        if self._memory_enabled:
            msg_id = self._chat_history_db.write_message(
                session_id=self._session_id,
                role="user",
                content=user_input,
                source=self._source,
            )
            self._last_history_id = max(self._last_history_id, int(msg_id))

        # 工作记忆并行总结
        summary_task: Optional[asyncio.Task] = None
        summary_recent_start: Optional[int] = None
        if self._memory_enabled and self._working_memory.check_threshold(
            actual_tokens=self._last_prompt_tokens
        ):
            result = self._working_memory.start_summarize(
                self._summary_llm_client, actual_tokens=self._last_prompt_tokens
            )
            if result:
                summary_task, summary_recent_start = result

        # 通过 AgentKernel 驱动 run_loop()
        hooks = AgentHooks(
            on_assistant_delta=on_stream_delta,
            on_reasoning_delta=on_reasoning_delta,
            on_trace_event=on_trace_event,
        )
        kernel = AgentKernel(tool_registry=self._tool_registry)
        run_result = await kernel.run(self, turn_id=turn_id, hooks=hooks)

        # 后处理
        await self._finalize_turn(run_result, summary_task, summary_recent_start)

        return run_result.output_text

    async def run_loop(
        self,
        turn_id: int = 0,
        hooks: Optional["AgentHooks"] = None,
    ):
        """
        AgentCore 主循环（async generator）。

        AgentCore 直接持有 LLMClient，在内部自旋完成多轮 LLM 推理——
        类比 CPU 自主执行指令流，无需每次都陷入 Kernel 态。

        只有两类操作会 yield 到 Kernel（系统调用）：
        - ToolCallAction  — 外部工具 IO，Kernel 统一执行
        - ReturnAction    — 本轮处理完成，交还控制权

        所有 logging / tracing 由本方法内部负责，因为 AgentCore
        是 LLM 调用的发起方，天然拥有完整的调用上下文。

        由 AgentKernel.run() 驱动，不应直接调用。
        """
        from agent_core.kernel_interface import (
            ReturnAction, ToolCallAction, ToolResultEvent, InternalLoader,
            ContextOverflowAction, ContextCompressedEvent,
        )

        loader = InternalLoader()
        iteration = 0

        while iteration < self._max_iterations:
            iteration += 1
            previous_messages = self._context.get_messages()

            try:
                # ── 组装 LLM Payload（Prompt + Context + Tools）──────────
                payload = loader.assemble(self)

                # Session 日志
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

                # Trace 事件
                await self._emit_trace(hooks, {
                    "type": "llm_request",
                    "turn_id": turn_id,
                    "iteration": iteration,
                    "tool_count": len(payload.tools),
                })

                # ── AgentCore 直接调用 LLM（CPU 自旋，无 Kernel 中介）───
                response = await self._llm_client.chat_with_tools(
                    system_message=payload.system,
                    messages=payload.messages,
                    tools=payload.tools,
                    tool_choice="auto",
                    on_content_delta=hooks.on_assistant_delta if hooks else None,
                    on_reasoning_delta=hooks.on_reasoning_delta if hooks else None,
                )

                if self._session_logger:
                    self._session_logger.on_llm_response(turn_id, iteration, response)

                # 累计 token 用量（AgentCore 内部状态，Kernel 在回收时读取）
                if response.usage:
                    pt, ct = response.usage.prompt_tokens, response.usage.completion_tokens
                    self._token_usage["prompt_tokens"] += pt
                    self._token_usage["completion_tokens"] += ct
                    self._token_usage["total_tokens"] += pt + ct
                    self._token_usage["call_count"] += 1
                    self._usage_calls.append((pt, ct))
                    self._last_prompt_tokens = pt

                # ── 处理工具调用 ─────────────────────────────────────────
                if response.tool_calls:
                    self._add_assistant_message_with_tool_calls(response)

                    for tool_call in response.tool_calls:
                        # Trace 事件（发出调用前记录）
                        await self._emit_trace(hooks, {
                            "type": "tool_call",
                            "turn_id": turn_id,
                            "iteration": iteration,
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "arguments": tool_call.arguments,
                        })
                        if self._session_logger:
                            self._session_logger.on_tool_call(
                                turn_id,
                                iteration,
                                ToolCall(
                                    id=tool_call.id,
                                    name=tool_call.name,
                                    arguments=tool_call.arguments or {},
                                ),
                            )

                        # 系统调用：委托 Kernel 执行工具 IO
                        t0 = time.perf_counter()
                        tool_event = yield ToolCallAction(
                            tool_call_id=tool_call.id,
                            tool_name=tool_call.name,
                            arguments=tool_call.arguments,
                        )
                        duration_ms = int((time.perf_counter() - t0) * 1000)

                        assert isinstance(tool_event, ToolResultEvent), \
                            f"run_loop: expected ToolResultEvent, got {type(tool_event)}"
                        result = tool_event.result

                        # Trace 事件（收到结果后记录，含耗时）
                        await self._emit_trace(hooks, {
                            "type": "tool_result",
                            "turn_id": turn_id,
                            "iteration": iteration,
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "success": result.success,
                            "message": result.message,
                            "duration_ms": duration_ms,
                            "error": result.error,
                        })
                        if self._session_logger:
                            self._session_logger.on_tool_result(
                                turn_id, iteration, tool_call.id, result, duration_ms
                            )

                        self._context.add_tool_result(tool_call.id, result)
                        self._queue_media_for_next_call(result)
                        self._collect_outgoing_attachment(result)

                        if self._memory_enabled:
                            msg_id = self._chat_history_db.write_message(
                                session_id=self._session_id,
                                role="tool",
                                content=result.to_json(),
                                tool_name=tool_call.name,
                                source=self._source,
                            )
                            self._last_history_id = max(self._last_history_id, int(msg_id))

                    continue

                # ── 最终响应，先检查上下文溢出再 ReturnAction ─────────────
                if response.content:
                    self._context.add_assistant_message(content=response.content)
                    if self._session_logger:
                        self._session_logger.on_assistant_message(turn_id, response.content)
                    if self._memory_enabled:
                        msg_id = self._chat_history_db.write_message(
                            session_id=self._session_id,
                            role="assistant",
                            content=response.content,
                            source=self._source,
                        )
                        self._last_history_id = max(self._last_history_id, int(msg_id))

                    # 上下文溢出检查（仅在完整 thought→tools→observations 循环结束后触发，
                    # 不在 tool_result 期间中断，保证工具调用链的完整性）
                    profile = getattr(self, "_core_profile", None)
                    threshold = profile.max_context_tokens if profile else None
                    if (
                        threshold is not None
                        and self._last_prompt_tokens is not None
                        and self._last_prompt_tokens >= threshold
                    ):
                        compress_event = yield ContextOverflowAction(
                            current_tokens=self._last_prompt_tokens,
                            threshold_tokens=threshold,
                            session_id=self._session_id,
                        )
                        # Kernel 完成压缩后发回 ContextCompressedEvent，Core 继续
                        if isinstance(compress_event, ContextCompressedEvent):
                            if compress_event.compressed_summary:
                                # 将 Kernel 生成的摘要写入工作记忆的 running_summary，
                                # 下一次 _build_system_prompt 时会自动注入到系统提示
                                self._working_memory._running_summary = compress_event.compressed_summary

                    yield ReturnAction(
                        message=response.content,
                        status="completed",
                        attachments=list(self._outgoing_attachments),
                    )
                    return

                # 没有内容也没有工具调用（降级）
                fallback = "抱歉，我无法处理您的请求。请重试或换一种方式表达。"
                if self._session_logger:
                    self._session_logger.on_assistant_message(turn_id, fallback)
                yield ReturnAction(message=fallback, status="fallback")
                return

            except asyncio.CancelledError:
                self._context.messages = previous_messages
                raise
            except Exception:
                self._context.messages = previous_messages
                raise

        # 超出最大迭代次数
        overflow_msg = "抱歉，处理您的请求时超出了最大迭代次数。请简化您的问题或稍后重试。"
        if self._session_logger:
            self._session_logger.on_assistant_message(turn_id, overflow_msg)
        yield ReturnAction(message=overflow_msg, status="overflow")

    @staticmethod
    async def _emit_trace(
        hooks: Optional["AgentHooks"],
        event: Dict[str, Any],
    ) -> None:
        """安全触发 on_trace_event 回调（支持 sync/async）。"""
        if hooks is None or hooks.on_trace_event is None:
            return
        maybe = hooks.on_trace_event(event)
        if inspect.isawaitable(maybe):
            await maybe

    async def _finalize_turn(
        self,
        run_result: "AgentRunResult",
        summary_task: Optional[asyncio.Task] = None,
        summary_recent_start: Optional[int] = None,
    ) -> None:
        """
        本轮后处理：合并工作记忆总结。

        由 process_input() 和 KernelScheduler._run_and_route() 在
        AgentKernel.run() 完成后调用。
        """
        if summary_task is not None and summary_recent_start is not None:
            summary_text = await summary_task
            self._working_memory.apply_summary(summary_text, summary_recent_start)

    def _build_system_prompt(self) -> str:
        """
        构建系统提示。

        当 source=="shuiyuan" 时使用水源专用 prompt，否则使用主 Agent prompt。
        """
        return build_agent_system_prompt(self)

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
        if self._kernel_enabled and tool_call.name not in self._current_visible_tools:
            return ToolResult(
                success=False,
                error="TOOL_NOT_VISIBLE",
                message=f"工具 '{tool_call.name}' 当前不在可见工作集中",
            )

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

        # 注入执行上下文（供 run_command 等工具做 select mode 鉴权）
        kwargs = dict(kwargs)
        kwargs["__execution_context__"] = {
            "tool_mode": self._effective_tool_mode,
            "source": self._source,
        }

        # 执行工具
        return await self._tool_registry.execute(tool_call.name, **kwargs)

    def _queue_media_for_next_call(self, result: ToolResult) -> None:
        """将工具结果中声明的媒体挂载到下一次 LLM 调用。"""
        queue_media_for_next_call(
            result,
            self._pending_multimodal_items,
            media_resolver=resolve_media_to_content_item,
        )

    def _collect_outgoing_attachment(self, result: ToolResult) -> None:
        """将工具结果中声明的「随回复发给用户的附件」加入本轮待发送列表。"""
        collect_outgoing_attachment(result, self._outgoing_attachments)

    def get_outgoing_attachments(self) -> List[Dict[str, Any]]:
        """返回本轮登记的要随回复一起发给用户的附件列表（只读副本）。"""
        return list(self._outgoing_attachments)

    def _append_pending_multimodal_messages(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        将待挂载媒体作为一条新的 user 多模态消息追加到当前请求。

        注意：这是一次性注入，不写入长期对话上下文，避免 data URL 污染历史消息。
        """
        return append_pending_multimodal_messages(messages, self._pending_multimodal_items)

    async def finalize_session(self) -> Optional[SessionSummary]:
        """
        会话结束时调用：总结会话并写入 recent_topic，不再使用 ShortTermMemory。

        Returns:
            生成的 SessionSummary，若记忆系统未启用或会话为空则返回 None
        """
        if not self._memory_enabled or self._current_turn_id == 0:
            return None

        summary_data = await self._working_memory.summarize_session(
            self._summary_llm_client
        )
        now_str = datetime.now(dt_timezone.utc).isoformat()

        session_summary = SessionSummary(
            session_id=self._session_id,
            time_start=self._session_start_time,
            time_end=now_str,
            summary=summary_data.get("summary", ""),
            decisions=summary_data.get("decisions", []),
            open_questions=summary_data.get("open_questions", []),
            referenced_files=summary_data.get("referenced_files", []),
            tags=summary_data.get("tags", []),
            turn_count=self._current_turn_id,
            token_usage=dict(self._token_usage),
        )

        # 将本次会话摘要写入 recent_topic（替代旧的 ShortTermMemory → distill 流程）
        owner_id: Optional[str] = None
        if self._source == "shuiyuan":
            owner_id = self._user_id
        self._long_term_memory.add_recent_topic(
            summary=session_summary.summary,
            session_id=self._session_id,
            tags=session_summary.tags,
            owner_id=owner_id,
        )

        return session_summary

    async def run_loop_kill(self):  # type: ignore[return]
        """
        Kill 专用 async generator。

        Kernel 发出 KillEvent 后调用此方法：
        Core 完成资源统计，yield CoreStatsAction，然后退出。
        Kernel 拿到 CoreStatsAction 后调用摘要器并完成进程回收。

        用法（由 AgentKernel.kill() 驱动）::

            gen = agent.run_loop_kill()
            action = await gen.__anext__()   # 拿到 CoreStatsAction
            # 不需要 asend，直接关闭 generator
        """
        from agent_core.kernel_interface.action import CoreStatsAction

        yield CoreStatsAction(
            token_usage=dict(self._token_usage),
            session_start_time=self._session_start_time,
            turn_count=self._current_turn_id,
            session_id=self._session_id,
        )

    async def activate_session(self, session_id: str, replay_messages_limit: Optional[int] = 0) -> None:
        """
        激活指定会话并尝试从持久化历史恢复上下文。

        用于跨终端切换到同一 session_id 时重建上下文。
        """
        sid = session_id.strip()
        if not sid:
            raise ValueError("session_id 不能为空")

        self._context.clear()
        self._session_id = sid
        self._session_start_time = datetime.now(dt_timezone.utc).isoformat()
        self._current_turn_id = 0
        self._last_prompt_tokens = None
        self._last_history_id = 0
        self.reset_token_usage()
        self._working_memory = WorkingMemory(
            context=self._context,
            max_tokens=self._config.memory.max_working_tokens,
            threshold=self._config.memory.working_summary_threshold,
            keep_recent=self._config.memory.working_keep_recent,
            hard_threshold_ratio=self._config.memory.working_summary_hard_ratio,
        )

        if not self._memory_enabled:
            return

        history = self._chat_history_db.get_session_messages(sid)
        if not history:
            return
        replay_rows = [r for r in history if r.get("role") in {"user", "assistant"}]
        if replay_messages_limit is not None and replay_messages_limit > 0:
            replay_rows = replay_rows[-replay_messages_limit:]
        elif replay_messages_limit is not None and replay_messages_limit <= 0:
            replay_rows = []
        for row in replay_rows:
            role = str(row.get("role", ""))
            content = str(row.get("content", ""))
            if role == "user":
                self._context.add_user_message(content)
            elif role == "assistant":
                self._context.add_assistant_message(content=content)
        self._current_turn_id = sum(1 for r in replay_rows if r.get("role") == "user")
        self._last_history_id = max(int(r.get("id", 0)) for r in history)
        if replay_rows:
            first_ts = replay_rows[0].get("timestamp")
            if isinstance(first_ts, str) and first_ts.strip():
                self._session_start_time = first_ts

    async def _sync_external_session_updates(self) -> None:
        """同步其他终端在同一 session 里新增的 user/assistant 消息。"""
        if not self._memory_enabled:
            return
        new_rows = self._chat_history_db.get_session_messages_after(
            self._session_id,
            self._last_history_id,
            roles=["user", "assistant"],
            limit=None,
        )
        if not new_rows:
            return
        for row in new_rows:
            role = str(row.get("role", ""))
            content = str(row.get("content", ""))
            if role == "user":
                self._context.add_user_message(content)
            elif role == "assistant":
                self._context.add_assistant_message(content=content)
        # 有外部新增时，强制让本轮阈值判断基于当前上下文重估，确保压缩及时触发。
        self._last_prompt_tokens = None
        self._last_history_id = max(self._last_history_id, max(int(r.get("id", 0)) for r in new_rows))

    def reset_session(self) -> None:
        """
        重置会话状态（用于 session 切分）：清空对话上下文，生成新的 session_id。
        调用方应先调用 finalize_session()，再调用此方法。
        """
        self._context.clear()
        self._session_id = new_session_id()
        self._last_history_id = 0
        self._session_start_time = datetime.now(dt_timezone.utc).isoformat()
        self._current_turn_id = 0
        self.reset_token_usage()
        # 清空工作记忆
        self._working_memory = WorkingMemory(
            context=self._context,
            max_tokens=self._config.memory.max_working_tokens,
            threshold=self._config.memory.working_summary_threshold,
            keep_recent=self._config.memory.working_keep_recent,
            hard_threshold_ratio=self._config.memory.working_summary_hard_ratio,
        )

    async def close(self) -> None:
        """关闭 Agent，释放资源"""
        await self._llm_client.close()
        if self._summary_llm_client is not self._llm_client:
            await self._summary_llm_client.close()
        if self._mcp_manager:
            await self._mcp_manager.close()
            self._mcp_manager = None
            self._mcp_connected = False
        self._chat_history_db.close()

    async def __aenter__(self) -> "ScheduleAgent":
        """异步上下文管理器入口"""
        if self._config.mcp.enabled and not self._mcp_connected:
            self._config.mcp.servers = self._build_runtime_mcp_servers(self._config.mcp.servers)
            self._mcp_manager = MCPClientManager(self._config.mcp)
            if not self._defer_mcp_connect:
                await self._mcp_manager.connect()
                self._tool_registry.update_tools(self._mcp_manager.get_proxy_tools())
                self._mcp_connected = True
        return self

    async def ensure_mcp_connected(self) -> bool:
        """若启用了 MCP 且为延迟连接，则执行连接并更新工具注册表。用于 daemon 启动后再连 MCP。"""
        if not self._config.mcp.enabled or self._mcp_connected or self._mcp_manager is None:
            return self._mcp_connected
        await self._mcp_manager.connect()
        self._tool_registry.update_tools(self._mcp_manager.get_proxy_tools())
        self._mcp_connected = True
        return True

    def _build_runtime_mcp_servers(self, servers: List[MCPServerConfig]) -> List[MCPServerConfig]:
        """
        构建运行期 MCP servers：
        - 保留用户配置
        - 若缺少本地 schedule_tools server，则自动补充
        """
        runtime_servers = [s.model_copy(deep=True) for s in servers]

        script_path = Path(__file__).resolve().parents[4] / "mcp_server.py"
        script_path_str = str(script_path)
        project_root = str(script_path.parent)
        project_src = str(script_path.parent / "src")

        has_local_server = any(
            (
                server.name == "schedule_tools"
                or (server.command in {"python", "python3", sys.executable} and script_path_str in server.args)
                or ("mcp_server.py" in server.args)
            )
            for server in runtime_servers
        )

        if not has_local_server:
            runtime_servers.append(
                MCPServerConfig(
                    name="schedule_tools",
                    enabled=True,
                    transport="stdio",
                    command=sys.executable,
                    args=[script_path_str],
                    env={
                        "PYTHONPATH": (
                            f"{project_src}:{os.environ.get('PYTHONPATH', '')}"
                            if os.environ.get("PYTHONPATH")
                            else project_src
                        )
                    },
                    cwd=project_root,
                    tool_name_prefix="mcp_local",
                    init_timeout_seconds=15,
                    call_timeout_seconds=self._config.mcp.call_timeout_seconds,
                )
            )

        return runtime_servers

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器退出"""
        await self.close()
