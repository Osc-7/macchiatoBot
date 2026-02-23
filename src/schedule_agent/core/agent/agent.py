"""
主 Agent 实现

实现基于工具驱动的 Agent 循环，支持多轮对话和工具调用。
集成四层记忆架构：工作记忆、短期记忆、长期记忆、内容记忆。
"""

import asyncio
import json
import time
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from schedule_agent.config import Config, MemoryConfig, get_config
from schedule_agent.core.context import ConversationContext, get_time_context
from schedule_agent.utils.billing import compute_cost_from_calls
from schedule_agent.core.orchestrator import ToolSnapshot, ToolWorkingSetManager
from schedule_agent.prompts import build_system_prompt as build_prompt
from schedule_agent.core.llm import LLMClient, LLMResponse, ToolCall, TokenUsage
from schedule_agent.core.tools import (
    BaseTool,
    CallToolTool,
    SearchToolsTool,
    ToolResult,
    VersionedToolRegistry,
)
from schedule_agent.core.memory import (
    WorkingMemory,
    ShortTermMemory,
    LongTermMemory,
    ContentMemory,
    RecallPolicy,
    RecallResult,
    SessionSummary,
)

if TYPE_CHECKING:
    from schedule_agent.utils.session_logger import SessionLogger


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
    ):
        """
        初始化 Agent。

        Args:
            config: 配置对象，如果为 None 则使用全局配置
            tools: 工具列表，如果为 None 则使用空注册表
            max_iterations: 最大工具调用迭代次数
            timezone: 时区
            session_logger: 会话日志记录器，用于记录完整 session 日志
        """
        self._config = config or get_config()
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
        self._kernel_enabled = (self._config.agent.tool_mode or "full").lower() == "kernel"
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

        # 四层记忆系统
        mem_cfg: MemoryConfig = self._config.memory
        self._memory_enabled = mem_cfg.enabled

        self._working_memory = WorkingMemory(
            context=self._context,
            max_tokens=mem_cfg.max_working_tokens,
            threshold=mem_cfg.working_summary_threshold,
            keep_recent=mem_cfg.working_keep_recent,
            hard_threshold_ratio=mem_cfg.working_summary_hard_ratio,
        )
        self._short_term_memory = ShortTermMemory(
            storage_dir=mem_cfg.short_term_dir,
            k=mem_cfg.short_term_k,
        )
        self._long_term_memory = LongTermMemory(
            storage_dir=mem_cfg.long_term_dir,
            memory_md_path=mem_cfg.memory_md_path,
            qmd_enabled=mem_cfg.qmd_enabled,
            qmd_command=mem_cfg.qmd_command,
        )
        self._content_memory = ContentMemory(
            content_dir=mem_cfg.content_dir,
            qmd_enabled=mem_cfg.qmd_enabled,
            qmd_command=mem_cfg.qmd_command,
        )
        self._recall_policy = RecallPolicy(
            force_recall=mem_cfg.force_recall,
            top_n=mem_cfg.recall_top_n,
            score_threshold=mem_cfg.recall_score_threshold,
        )

        # 注册工具
        if tools:
            for tool in tools:
                self._tool_registry.register(tool)

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

    def get_token_usage(self) -> dict:
        """
        获取本会话累计的 token 用量。

        Returns:
            包含 prompt_tokens, completion_tokens, total_tokens, call_count, cost_yuan 的字典
        """
        out: dict[str, int | float] = dict(self._token_usage)
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
        # 0. 递增轮次并记录用户消息
        self._current_turn_id += 1
        turn_id = self._current_turn_id

        # 0.5 记忆检索 enrich（在添加用户消息之前收集上下文）
        # 使用 to_thread 避免同步 recall 阻塞事件循环，确保 spinner 能及时显示
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

        # 1. 添加用户消息到上下文
        self._context.add_user_message(user_input)

        if self._session_logger:
            self._session_logger.on_user_message(turn_id, user_input)

        # 1.5 工作记忆：若超阈值则启动并行总结（与 LLM 对话同时执行，结束后合并）
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

        # 2. Agent 主循环
        final_response: Optional[str] = None
        iteration = 0
        while iteration < self._max_iterations:
            iteration += 1

            system_prompt = self._build_system_prompt()
            messages = self._context.get_messages()
            if self._kernel_enabled:
                self._last_snapshot = self._working_set.build_snapshot(self._tool_registry)
                tools_defs = self._last_snapshot.openai_tools
                self._current_visible_tools = set(self._last_snapshot.tool_names)
            else:
                tools_defs = self._tool_registry.get_all_definitions()
                self._current_visible_tools = set(self._tool_registry.list_names())

            if self._session_logger:
                self._session_logger.on_llm_request(
                    turn_id=turn_id,
                    iteration=iteration,
                    message_count=len(messages),
                    tool_count=len(tools_defs),
                    system_prompt_len=len(system_prompt),
                    system_prompt=system_prompt if self._session_logger.enable_detailed_log else None,
                    messages=messages if self._session_logger.enable_detailed_log else None,
                )

            # 2.1 调用 LLM
            response = await self._llm_client.chat_with_tools(
                system_message=system_prompt,
                messages=messages,
                tools=tools_defs,
                tool_choice="auto",
            )

            if self._session_logger:
                self._session_logger.on_llm_response(turn_id, iteration, response)

            # 累计 token 用量（含 per-call 记录用于阶梯计费）
            if response.usage:
                pt, ct = response.usage.prompt_tokens, response.usage.completion_tokens
                self._token_usage["prompt_tokens"] += pt
                self._token_usage["completion_tokens"] += ct
                self._token_usage["total_tokens"] += pt + ct
                self._token_usage["call_count"] += 1
                self._usage_calls.append((pt, ct))
                self._last_prompt_tokens = pt

            # 2.2 处理工具调用
            if response.tool_calls:
                # 添加助手消息（包含工具调用）
                self._add_assistant_message_with_tool_calls(response)

                # 执行所有工具调用
                for tool_call in response.tool_calls:
                    if self._session_logger:
                        self._session_logger.on_tool_call(turn_id, iteration, tool_call)
                    t0 = time.perf_counter()
                    result = await self._execute_tool_call(tool_call)
                    duration_ms = int((time.perf_counter() - t0) * 1000)
                    if self._session_logger:
                        self._session_logger.on_tool_result(
                            turn_id, iteration, tool_call.id, result, duration_ms
                        )
                    self._context.add_tool_result(tool_call.id, result)

                # 继续循环，让 LLM 处理工具结果
                continue

            # 2.3 最终响应
            if response.content:
                self._context.add_assistant_message(content=response.content)
                if self._session_logger:
                    self._session_logger.on_assistant_message(turn_id, response.content)
                final_response = response.content
                break

            # 如果没有内容也没有工具调用，返回默认响应
            fallback = "抱歉，我无法处理您的请求。请重试或换一种方式表达。"
            if self._session_logger:
                self._session_logger.on_assistant_message(turn_id, fallback)
            final_response = fallback
            break

        # 2.4 合并并行总结结果（若已启动）
        if summary_task is not None and summary_recent_start is not None:
            summary_text = await summary_task
            self._working_memory.apply_summary(summary_text, summary_recent_start)

        if final_response is not None:
            return final_response

        # 超过最大迭代次数
        overflow_msg = "抱歉，处理您的请求时超出了最大迭代次数。请简化您的问题或稍后重试。"
        if self._session_logger:
            self._session_logger.on_assistant_message(turn_id, overflow_msg)
        return overflow_msg

    def _build_system_prompt(self) -> str:
        """
        构建系统提示。

        从 prompts/system/ 加载片段并组合，包含：
        - Agent 身份和能力说明
        - 当前时间上下文
        - 工具使用指南
        - 记忆上下文（若有）

        Returns:
            系统提示字符串
        """
        time_ctx = get_time_context(self._timezone)
        prompt = build_prompt(
            time_context=time_ctx.to_prompt_string(),
            config=self._config,
            has_web_extractor=self._tool_registry.has("extract_web_content"),
            has_file_tools=self._tool_registry.has("read_file"),
            tool_mode=self._config.agent.tool_mode or "full",
        )

        if self._memory_enabled:
            parts: List[str] = []
            # 短期会话和 MEMORY.md 直接加载，无需检索
            short_recent = self._short_term_memory.get_recent(
                self._config.memory.recall_top_n
            )
            if short_recent:
                parts.append("## 近期会话记忆")
                for s in reversed(short_recent):
                    parts.append(f"- [{s.session_id}] {s.summary}")
            md_content = self._long_term_memory.read_memory_md()
            if md_content and len(md_content) > 50:
                excerpt = md_content if len(md_content) <= 1000 else md_content[:1000] + "\n..."
                parts.append("\n## 核心记忆 (MEMORY.md)")
                parts.append(excerpt)
            recall_ctx = getattr(self, "_last_recall_result", RecallResult())
            recall_text = recall_ctx.to_context_string()
            if recall_text:
                parts.append(f"\n{recall_text}")
            if parts:
                prompt += "\n\n# 记忆上下文\n\n" + "\n".join(parts)

            if self._working_memory.running_summary:
                prompt += (
                    f"\n\n# 工作记忆摘要\n\n{self._working_memory.running_summary}"
                )

        return prompt

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

        # 执行工具
        return await self._tool_registry.execute(tool_call.name, **kwargs)

    async def finalize_session(self) -> Optional[SessionSummary]:
        """
        会话结束时调用：总结会话并写入短期记忆，触发出队提炼。

        Returns:
            生成的 SessionSummary，若记忆系统未启用或会话为空则返回 None
        """
        if not self._memory_enabled or self._current_turn_id == 0:
            return None

        summary_data = await self._working_memory.summarize_session(
            self._summary_llm_client
        )
        session_id = f"sess-{int(time.time())}"
        now_str = datetime.now(dt_timezone.utc).isoformat()

        session_summary = SessionSummary(
            session_id=session_id,
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

        evicted = self._short_term_memory.add(session_summary)

        if evicted:
            await self._long_term_memory.distill(evicted, self._summary_llm_client)

        return session_summary

    async def close(self) -> None:
        """关闭 Agent，释放资源"""
        await self._llm_client.close()
        if self._summary_llm_client is not self._llm_client:
            await self._summary_llm_client.close()

    async def __aenter__(self) -> "ScheduleAgent":
        """异步上下文管理器入口"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器退出"""
        await self.close()
