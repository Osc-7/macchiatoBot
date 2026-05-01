"""
主 Agent 实现

实现基于工具驱动的 Agent 循环，支持多轮对话和工具调用。
集成四层记忆架构：工作记忆、短期记忆、长期记忆、内容记忆。
"""

import asyncio
import inspect
import json
import logging
import os
import sys
import time
from datetime import datetime
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, cast

from agent_core.config import Config, MemoryConfig, MCPServerConfig, get_config
from agent_core.kernel_interface.profile import core_profile_to_checkpoint_dict
from agent_core.context import ConversationContext, apply_user_message_time_prefix
from agent_core.utils.billing import compute_cost_from_calls
from agent_core.mcp import MCPClientManager
from agent_core.orchestrator import ToolSnapshot, ToolWorkingSetManager
from agent_core.llm import (
    LLMClient,
    LLMResponse,
    ToolCall,
)
from agent_core.utils.media import resolve_media_to_content_item
from agent_core.tools import (
    BaseTool,
    CallToolTool,
    SearchToolsTool,
    ToolResult,
    VersionedToolRegistry,
)
from system.tools.chat_history_tools import (
    ChatSearchTool,
    ChatContextTool,
    ChatScrollTool,
)
from system.tools.web_extractor_tool import WebExtractorTool
from system.tools.web_search_tool import WebSearchTool
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
from .checkpoint import CoreCheckpoint, CoreCheckpointManager
from .memory_paths import new_session_id, resolve_memory_owner_paths
from .prompt_builder import build_agent_system_prompt

if TYPE_CHECKING:
    from agent_core.interfaces import AgentHooks, AgentRunResult
    from agent_core.utils.session_logger import SessionLogger


def _format_subagent_limit_msg(
    *,
    reason: str,
    subagent_id: str,
    log_path: Optional[Path] = None,
    log_dir: str = "./logs/sessions",
    limit_type: str,
) -> str:
    """构建子任务被系统限制终止时的完整提示，含取消原因、日志位置与主 Agent 建议。"""
    if log_path is not None:
        path_hint = str(log_path)
    else:
        path_hint = f"{log_dir}/session-subagent:{subagent_id}-*.jsonl（可用 ls -t ... | head -1 取最新）"
    return (
        f"[子任务 {subagent_id} 被系统终止]\n\n"
        f"**取消原因**: {reason}\n\n"
        f"**日志位置**: {path_hint}\n\n"
        f"**建议主 Agent**: 使用 bash 执行 `tail -n 100 <日志路径>` 读取日志尾部，"
        f"检查子任务进展后决定是否调整 config 中的 {limit_type} 限额并重启子任务。"
    )


logger = logging.getLogger(__name__)


# region agent log
def _agent_debug_log(hypothesis_id: str, location: str, message: str, data: Dict[str, Any]) -> None:
    """Append minimal NDJSON debug evidence for the current Cursor debug session."""
    try:
        payload = {
            "sessionId": "972f2b",
            "runId": "pre-fix",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open("/home/ubuntu/macchiatoBot/.cursor/debug-972f2b.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
# endregion


def _debug_context_reasoning_stats(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "total": len(messages),
        "assistant_total": 0,
        "assistant_with_tool_calls": 0,
        "assistant_missing_reasoning": 0,
        "assistant_tool_missing_reasoning": 0,
        "missing_indexes": [],
        "tool_missing_indexes": [],
    }
    for idx, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        stats["assistant_total"] += 1
        has_tools = bool(msg.get("tool_calls"))
        if has_tools:
            stats["assistant_with_tool_calls"] += 1
        if "reasoning_content" not in msg:
            stats["assistant_missing_reasoning"] += 1
            stats["missing_indexes"].append(idx)
            if has_tools:
                stats["assistant_tool_missing_reasoning"] += 1
                stats["tool_missing_indexes"].append(idx)
    return stats


def _default_token_usage_dict() -> dict[str, int]:
    """会话累计用量默认值（含 DeepSeek prompt_cache_* 累计）。"""
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "call_count": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
    }


_MISSING_TOOL_REASONING_RETRY_LIMIT = 1


def _should_attach_user_time_stamp(
    effective_text: str,
    effective_media_items: Optional[List[Dict[str, Any]]],
) -> bool:
    """仅在确有发往模型的正文或多模态时附加时间前缀（避免仅 defer 的空 user 条出现孤立前缀）。"""
    if effective_media_items:
        return True
    return bool((effective_text or "").strip())


def _pricing_map_for_active_provider(agent: Any) -> Optional[dict[str, Any]]:
    """返回当前 active provider 的局部价表，供 billing 按 model ID 匹配。"""
    try:
        name = agent._llm_client.active_provider_name
        entry = agent._config.llm.providers.get(name)
        pricing = getattr(entry, "pricing", None) if entry is not None else None
        model = getattr(entry, "model", None) if entry is not None else None
        if pricing is not None and model:
            return {str(model): pricing}
    except Exception:
        return None
    return None


class AgentCore:
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
        tool_catalog: Optional[VersionedToolRegistry] = None,
        max_iterations: Optional[int] = 10,
        timezone: str = "Asia/Shanghai",
        session_logger: Optional["SessionLogger"] = None,
        user_id: str = "root",
        source: str = "cli",
        defer_mcp_connect: bool = False,
        *,
        memory_enabled: Optional[bool] = None,
        core_profile: Optional[Any] = None,
    ):
        """
        初始化 Agent。

        Args:
            config: 配置对象，如果为 None 则使用全局配置
            tools: 工具列表，如果为 None 则使用空注册表
            tool_catalog: 完整工具 catalog（供 search_tools / call_tool 搜索与按名调用全局工具）
            max_iterations: 最大工具调用迭代次数
            timezone: 时区
            session_logger: 会话日志记录器，用于记录完整 session 日志
            user_id: 记忆命名空间用户 ID（同一 user_id 可跨终端共享记忆）
            source: 来源命名空间（如 cli/qq/whatsapp）
            defer_mcp_connect: 为 True 时 __aenter__ 不连接 MCP，需稍后调用 ensure_mcp_connected()（用于 daemon 先完成启动再连 MCP）
            memory_enabled: 覆盖配置级 memory.enabled，用于按 Core 粒度关闭记忆（例如 cron/heartbeat）
            core_profile: Kernel 侧 CoreProfile（需在 __aenter__ 前传入，以便 bash 工作区与权限与 Core 一致）
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
        self._tool_catalog = tool_catalog
        self._context = ConversationContext()
        self._max_iterations = max_iterations
        self._timezone = timezone
        self._session_logger = session_logger
        agent_cfg = self._config.agent
        from agent_core.agent.working_set_pins import compute_pinned_tool_names_for_core

        deduped_pinned = compute_pinned_tool_names_for_core(
            self._config, core_profile, self._source
        )
        self._working_set = ToolWorkingSetManager(
            pinned_tools=deduped_pinned,
            working_set_size=self._config.agent.working_set_size,
        )
        self._last_snapshot = ToolSnapshot(version=-1, tool_names=[], openai_tools=[])
        self._current_visible_tools: set[str] = set()
        self._pending_multimodal_items: List[Dict[str, Any]] = []
        # 前端（如飞书）可将媒体标记为 defer_with_next_user_input，
        # 这些内容会缓存到下一次“带文本的用户输入”再注入，避免收到图片即立刻触发分析。
        self._pending_user_multimodal_items: List[Dict[str, Any]] = []
        # 当主模型不具备 vision 时，对图片做文字占位降级（见 media_helpers.append_pending_multimodal_messages），
        # 同时把原始媒体登记到这里供 recognize_image 工具按 name/path 回查。
        self._last_unseen_media: List[Dict[str, Any]] = []
        # 本轮回复要附带发给用户的图片等附件（由 attach_image_to_reply 等工具登记）
        self._outgoing_attachments: List[Dict[str, Any]] = []
        # 本会话 token 用量累计
        self._token_usage = _default_token_usage_dict()
        # 每次调用的 (prompt_tokens, completion_tokens, cache_hit, cache_miss)，用于计费
        self._usage_calls: List[tuple[int, int, int, int]] = []
        # 上一轮 LLM 的 prompt_tokens，供工作记忆阈值判断
        self._last_prompt_tokens: Optional[int] = None
        # 最近一次 memory recall 结果（每轮 prepare_turn 更新；__init__ 必须初始化，
        # 否则 _build_system_prompt 在首次 prepare_turn 前调用时会触发 AttributeError）
        self._last_recall_result: RecallResult = RecallResult()
        # 当前轮次（每次 process_input 递增）
        self._current_turn_id = 0
        # 会话起始时间
        self._session_start_time = datetime.now(dt_timezone.utc).isoformat()
        # 会话 ID（用于 ChatHistoryDB 写入分组）
        self._session_id = new_session_id()
        # ChatHistoryDB 最后同步到的消息 ID（用于跨终端增量同步）
        self._last_history_id: int = 0
        # CoreProfile — Kernel 注入的权限配置；None 表示无限制（向后兼容）
        self._core_profile: Optional[Any] = core_profile

        # 四层记忆系统
        mem_cfg: MemoryConfig = self._config.memory
        # 允许按 CoreProfile 粒度覆写 memory.enabled（例如 cron/heartbeat 不落盘）
        self._memory_enabled = (
            mem_cfg.enabled if memory_enabled is None else bool(memory_enabled)
        )

        # 工作记忆仅依赖内存中的对话上下文，不触发任何磁盘目录创建，始终可用。
        self._working_memory = WorkingMemory(
            context=self._context,
            max_tokens=mem_cfg.max_working_tokens,
        )
        self._recall_policy = RecallPolicy(
            force_recall=mem_cfg.force_recall,
            top_n=mem_cfg.recall_top_n,
            score_threshold=mem_cfg.recall_score_threshold,
        )

        # 持久化记忆（长期 / 内容 / 对话历史）仅在 memory_enabled 为真时才初始化，
        # 以避免为每个 cron:{job} / heartbeat Core 创建独立 data/memory/{source}/{user}/ 目录。
        self._long_term_memory: Optional[LongTermMemory]
        self._content_memory: Optional[ContentMemory]
        self._chat_history_db: Optional[ChatHistoryDB]
        self._checkpoint_manager: Optional[CoreCheckpointManager] = None
        if self._memory_enabled:
            source_paths = resolve_memory_owner_paths(
                mem_cfg, self._user_id, config=self._config, source=self._source
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
            self._chat_history_db = ChatHistoryDB(
                source_paths["chat_history_db_path"],
                default_source=None,
            )
            self._checkpoint_manager = CoreCheckpointManager(
                source_paths["checkpoint_path"]
            )
        else:
            self._long_term_memory = None
            self._content_memory = None
            self._chat_history_db = None

        # 注册工具
        if tools:
            for tool in tools:
                self._tool_registry.register(tool)
            if self._core_profile is None:
                # 向后兼容：无 CoreProfile 的独立 Agent（常见于单元测试/本地直连）
                # 默认将显式传入的工具加入工作集，避免需要额外模板配置。
                self._working_set.add_to_working_set([tool.name for tool in tools])

        # 注册对话历史检索工具
        if self._memory_enabled and self._chat_history_db is not None:
            db = self._chat_history_db
            for chat_tool in [
                ChatSearchTool(db),
                ChatContextTool(db),
                ChatScrollTool(db),
            ]:
                if not self._tool_registry.has(chat_tool.name):
                    self._tool_registry.register(chat_tool)

        # Meta：search_tools / call_tool 对所有 Core 注册；
        # search/call 默认连接完整工具 catalog，当前 Core 的 registry 仍用于真实暴露与执行。
        if not self._tool_registry.has("search_tools"):
            self._tool_registry.register(
                SearchToolsTool(
                    registry=self._tool_catalog or self._tool_registry,
                    working_set=self._working_set,
                    profile_getter=lambda: getattr(self, "_core_profile", None),
                )
            )
        if not self._tool_registry.has("call_tool"):
            self._tool_registry.register(
                CallToolTool(
                    registry=self._tool_catalog or self._tool_registry,
                    profile_getter=lambda: getattr(self, "_core_profile", None),
                )
            )
        if not self._tool_registry.has("request_permission"):
            from agent_core.tools.request_permission_tool import RequestPermissionTool

            self._tool_registry.register(RequestPermissionTool())
        if not self._tool_registry.has("ask_user"):
            from agent_core.tools.ask_user_tool import AskUserTool

            self._tool_registry.register(AskUserTool())

        # MCP 客户端（在 __aenter__ 中连接，或 defer 时由 ensure_mcp_connected 连接）
        self._mcp_manager: Optional[MCPClientManager] = None
        self._mcp_connected = False
        self._defer_mcp_connect = defer_mcp_connect
        # 为 True 表示 MCP 已由“连接所在任务”关闭（用于 daemon 后台任务同任务 close，避免 anyio 跨任务 exit）
        self._mcp_closed_by_owner = False

        # 持久化 Bash 会话（在 __aenter__ 中启动，与 search_tools/call_tool 同为 Core 自注册 meta tool）
        self._bash: Optional["BashRuntime"] = None
        self._bash_security: Optional["BashSecurity"] = None

        # 联网工具（基于 Tavily MCP）
        if self._config.mcp.enabled:
            if not self._tool_registry.has("web_search"):
                self._tool_registry.register(
                    WebSearchTool(registry=self._tool_registry)
                )
            if not self._tool_registry.has("extract_web_content"):
                self._tool_registry.register(
                    WebExtractorTool(registry=self._tool_registry)
                )

        # 识图回落工具：仅当配置了 vision_provider（即存在可落地的视觉能力）时才注册；
        # 未配置时即便主模型没有 vision 也无处回落，避免向 LLM 暴露一个会报错的工具。
        # pin/unpin 由 _refresh_recognize_image_visibility 控制，运行时 /model 切换后立即生效。
        if self._llm_client.vision_provider_name and not self._tool_registry.has(
            "recognize_image"
        ):
            from system.tools.recognize_image_tool import RecognizeImageTool

            self._tool_registry.register(
                RecognizeImageTool(
                    llm_client=self._llm_client,
                    config=self._config,
                    unseen_media=self._last_unseen_media,
                )
            )
        self._refresh_recognize_image_visibility()

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

    def list_models(self) -> List[Dict[str, Any]]:
        """列出当前 LLMClient 可用的 provider 及其能力，给 CLI / IPC 用。"""
        out: List[Dict[str, Any]] = []
        try:
            prov_cfg = getattr(self._config.llm, "providers", {}) or {}
            for name, caps in self._llm_client.list_models():
                api_model = self._llm_client.providers[name].model
                entry = prov_cfg.get(name)
                label = getattr(entry, "label", None) if entry is not None else None
                base_url = getattr(entry, "base_url", None) if entry is not None else None
                out.append(
                    {
                        # 配置名：传给 /model <name> 的标识
                        "name": name,
                        # 厂商 API 请求的模型 ID（标准名），与 ProviderEntry.model 一致
                        "model": api_model,
                        "api_model": api_model,
                        "label": label,
                        "base_url": base_url,
                        "vision": bool(getattr(caps, "vision", False)),
                        "function_calling": bool(getattr(caps, "function_calling", True)),
                        "reasoning_content": bool(
                            getattr(caps, "reasoning_content", False)
                        ),
                        "context_window": getattr(caps, "context_window", None),
                        "is_active": name == self._llm_client.active_provider_name,
                        "is_vision_provider": name == self._llm_client.vision_provider_name,
                    }
                )
        except Exception as exc:
            logger.warning("list_models failed: %s", exc)
        return out

    def switch_model(self, name: str) -> Dict[str, Any]:
        """
        运行时切换主对话 LLM provider。

        切换后会立即刷新 recognize_image 工具的可见性，确保下一轮对话使用新的能力集。

        Returns:
            dict: 包含 ``name`` / ``model`` / 切换后主模型 ``vision`` 能力等信息。
        """
        self._llm_client.switch_model(name)
        self._refresh_recognize_image_visibility()
        caps = self._llm_client.capabilities
        api_model = self._llm_client.model
        return {
            "name": self._llm_client.active_provider_name,
            "model": api_model,
            "api_model": api_model,
            "vision": bool(getattr(caps, "vision", False)),
            "vision_provider": self._llm_client.vision_provider_name,
        }

    def _refresh_recognize_image_visibility(self) -> None:
        """
        根据当前活跃 provider 的 vision 能力和可用 vision_provider，
        动态 pin/unpin `recognize_image` 工具。

        - 主模型具备 vision：工具无用，移出 pinned（不进工作集）。
        - 主模型无 vision 且配置了 vision_provider：加入 pinned，保证每轮都对 LLM 可见。
        - 主模型无 vision 且没有 vision_provider：移出 pinned（回落也不可用，避免误导）。
        """
        try:
            caps = self._llm_client.capabilities
            main_has_vision = bool(getattr(caps, "vision", False))
            vp = self._llm_client.vision_provider_name
        except Exception:
            return

        if not main_has_vision and vp:
            self._working_set.pin("recognize_image")
        else:
            self._working_set.unpin("recognize_image")

    def _should_retry_missing_tool_reasoning(self, response: LLMResponse) -> bool:
        """
        是否应丢弃本次响应并重试。

        某些 reasoning-capable 模型在 thinking + tool_calls 场景下会偶发返回
        ``tool_calls`` 但缺少 ``reasoning_content``。若把该响应写入历史，下一轮
        回传给 DeepSeek/Kimi/GLM 等端点时可能触发 400，并污染整段会话。
        """
        if not response.tool_calls:
            return False
        caps = getattr(self._llm_client, "capabilities", None)
        if getattr(caps, "reasoning_content", False) is not True:
            return False
        provider = None
        try:
            provider = self._llm_client._active_provider()
        except Exception:
            provider = None
        model = str(getattr(provider, "model", "") or "").strip().lower()
        vendor_params = getattr(provider, "_vendor_params", None)
        thinking_enabled = False
        if isinstance(vendor_params, dict):
            th = vendor_params.get("thinking")
            if isinstance(th, dict):
                thinking_enabled = (
                    str(th.get("type", "")).strip().lower() == "enabled"
                )
            if not thinking_enabled:
                out_cfg = vendor_params.get("output_config")
                if isinstance(out_cfg, dict) and str(out_cfg.get("effort", "")).strip():
                    thinking_enabled = True
        if not thinking_enabled and "reasoner" not in model:
            return False
        rc = response.reasoning_content
        if isinstance(rc, str):
            return not rc.strip()
        return rc is None

    def _looks_like_deepseek_api(self, provider: Any) -> bool:
        """配置文件里 provider key / model 可能被写成非 deepseek 字样，但 base_url 仍会暴露真实端点。"""
        if provider is None:
            return False
        name = str(getattr(provider, "name", "") or "").lower()
        model = str(getattr(provider, "model", "") or "").lower()
        base = str(getattr(provider, "_base_url", "") or "").lower()
        return "deepseek" in name or "deepseek" in model or "deepseek" in base

    @staticmethod
    def _looks_like_vendor_excluded_from_ds_empty_reasoning(provider: Any) -> bool:
        """对明确非 DeepSeek 的厂商不重试写入空 reasoning（例如 Kimi/GPT）。"""
        model = str(getattr(provider, "model", "") or "").lower()
        base = str(getattr(provider, "_base_url", "") or "").lower()
        name = str(getattr(provider, "name", "") or "").lower()
        hay = f"{model} {base} {name}"
        for tok in ("kimi", "moonshot", "openai.com", "api.openai", "gpt-4", "gpt-5", "glm", "dashscope"):
            if tok in hay:
                return True
        return False

    def _looks_like_deepseek_usage_cache_split(self, response: LLMResponse) -> bool:
        """会话日志里 DS 计费会给出同时非零或可区分的 prompt_cache_hit/miss（区别于多数网关全 0）。"""
        u = getattr(response, "usage", None)
        if u is None:
            return False
        try:
            hit = int(getattr(u, "prompt_cache_hit_tokens", None) or 0)
            miss = int(getattr(u, "prompt_cache_miss_tokens", None) or 0)
        except (TypeError, ValueError):
            return False
        return (hit + miss) > 0

    def _provider_eligible_for_empty_reasoning_synthesis(self, response: LLMResponse) -> bool:
        """是否应对缺字段的合成 ``reasoning_content=\"\"``（provider 画像与用量启发式）。"""
        provider = None
        try:
            provider = self._llm_client._active_provider()
        except Exception:
            provider = None
        if provider is None:
            return False
        if self._looks_like_deepseek_api(provider):
            return True
        if self._looks_like_vendor_excluded_from_ds_empty_reasoning(provider):
            return False
        if type(provider).__name__ != "OpenAICompatProvider":
            return False
        return self._looks_like_deepseek_usage_cache_split(response)

    def _thinking_final_response_missing_reasoning_content(self, response: LLMResponse) -> bool:
        """thinking + 仅有正文、无 tool_calls 时，模型也可能不返回 ``reasoning_content``。"""
        if response.tool_calls:
            return False
        caps = getattr(self._llm_client, "capabilities", None)
        if getattr(caps, "reasoning_content", False) is not True:
            return False
        if not str(response.content or "").strip():
            return False
        provider = None
        try:
            provider = self._llm_client._active_provider()
        except Exception:
            provider = None
        model = str(getattr(provider, "model", "") or "").strip().lower()
        vendor_params = getattr(provider, "_vendor_params", None)
        thinking_enabled = False
        if isinstance(vendor_params, dict):
            th = vendor_params.get("thinking")
            if isinstance(th, dict):
                thinking_enabled = (
                    str(th.get("type", "")).strip().lower() == "enabled"
                )
            if not thinking_enabled:
                out_cfg = vendor_params.get("output_config")
                if isinstance(out_cfg, dict) and str(out_cfg.get("effort", "")).strip():
                    thinking_enabled = True
        if not thinking_enabled and "reasoner" not in model:
            return False
        rc = response.reasoning_content
        if isinstance(rc, str):
            return not rc.strip()
        return rc is None

    def _should_synthesize_empty_final_reasoning(self, response: LLMResponse) -> bool:
        if not self._thinking_final_response_missing_reasoning_content(response):
            return False
        return self._provider_eligible_for_empty_reasoning_synthesis(response)

    def _should_synthesize_empty_tool_reasoning(self, response: LLMResponse) -> bool:
        """
        DeepSeek thinking + tool_calls 偶发不返回 ``reasoning_content``。

        DeepSeek API 的实际校验要求是字段存在；直连探针已验证空字符串可通过。
        因此对 DeepSeek 缺字段响应合成 ``reasoning_content=""``，避免重试同一
        payload 反复得到同样的不完整响应并中断本轮。
        """
        if not self._should_retry_missing_tool_reasoning(response):
            return False
        return self._provider_eligible_for_empty_reasoning_synthesis(response)

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
        return self._require_chat_history_db().delete_session_messages(
            sid, source=self._source
        )

    async def compress_context(
        self,
        *,
        keep_recent_turns: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        手动触发上下文压缩（``/compress`` 命令路径）。

        与自动触发（``ContextOverflowAction``）走完全相同的 Kernel 逻辑，**不是**
        另起一套独立实现，因此对 ``running_summary`` / ``compression_round`` /
        ``[会话进行中摘要]`` 注入都是同一种行为。

        典型用途：

        - 切换到更小窗口模型前先压一下，避免下一轮 LLM 直接超窗。
        - 长会话主动瘦身，缩短后续 prompt、降低费用。
        - 验证 / 调试上下文压缩链路。

        Args:
            keep_recent_turns: 保留最近 N 个完整 user 轮次；None 则取 profile / 全局默认。

        Returns:
            结构化结果字典，前端可直接渲染：

            ============================  ===========================================
            字段                           含义
            ============================  ===========================================
            ``compressed``                 是否实际发生压缩（消息数变少 或 产出摘要）
            ``summary``                    本次摘要正文（无 summary LLM 时为空字符串）
            ``summary_chars``              摘要字符数
            ``messages_before`` / ``_after`` 压缩前后 messages 条数
            ``current_tokens``             触发时上下文估算 token 数
            ``threshold_tokens``           当前阈值（即 ``_compute_compress_threshold``）
            ``compression_round``          已完成的压缩轮次
            ``model``                      触发时活跃 provider 的 API 模型 ID
            ============================  ===========================================
        """
        from system.kernel import AgentKernel

        before = len(self._context.get_messages())
        current_tokens = self._estimate_current_context_tokens()
        threshold = self._compute_compress_threshold()

        summary, kept = await AgentKernel.compress_context(
            self, keep_recent_turns=keep_recent_turns
        )

        after = len(self._context.get_messages())
        # 压缩后下一轮 LLM 的 prompt_tokens 会显著变化；清掉缓存的提示，
        # 让 _compute_compress_threshold / get_token_usage 在下一轮基于真实计数。
        self._last_prompt_tokens = None

        try:
            active_model = self._llm_client.model
        except Exception:
            active_model = ""

        return {
            "compressed": (after < before) or bool((summary or "").strip()),
            "summary": summary or "",
            "summary_chars": len(summary or ""),
            "messages_before": before,
            "messages_after": after,
            "kept": kept,
            "current_tokens": int(current_tokens or 0),
            "threshold_tokens": int(threshold),
            "compression_round": self._working_memory.compression_round,
            "model": active_model,
        }

    def _compute_effective_max_tokens(self, payload: Any) -> Optional[int]:
        """
        计算本次 ``chat_with_tools`` 的 completion 预算（``max_tokens``）。

        部分 provider（OpenAI 兼容、Kimi 等）会把 ``max_tokens`` 视作「必须从
        context_window 里预留的额度」——例如配置 ``max_tokens=65536`` 而模型窗口
        128k 时，prompt 只剩 ~62k 可用；prompt 一旦更大就会以 400 报错
        ``maximum context length is X. However, you requested ...``。

        本方法把预算收紧为
        ``min(provider 配置 max_tokens, context_window − estimated_prompt − safety)``，
        让 completion 跟着 prompt 大小自动让位；返回值会作为 ``max_tokens_override``
        传给 provider，``None`` 表示按 provider 构造期固定值走（无法估算时回退）。

        ``payload`` 是 ``InternalLoader.assemble`` 的产物，含 ``system`` / ``messages``
        / ``tools``。估算口径与 ``estimate_messages_tokens`` 一致。
        """
        try:
            cw = int(getattr(self._llm_client, "context_window", 0) or 0)
        except Exception:
            cw = 0
        try:
            cfg_max = int(getattr(self._llm_client, "max_tokens", 0) or 0)
        except Exception:
            cfg_max = 0

        if cw <= 0:
            # 没有窗口信息，无法判断，按构造期固定值；不返回 override
            return None

        # 估算 prompt = system + messages + tools schema
        try:
            from agent_core.memory.working_memory import (
                estimate_messages_tokens,
                estimate_tokens,
            )
            est = 0
            sys_text = getattr(payload, "system", None) or ""
            est += estimate_tokens(sys_text)
            msgs = getattr(payload, "messages", None) or []
            est += estimate_messages_tokens(msgs)
            tools = getattr(payload, "tools", None) or []
            if tools:
                import json as _json

                est += estimate_tokens(_json.dumps(tools, ensure_ascii=False))
        except Exception:
            return None

        safety_margin = 1024
        budget = cw - est - safety_margin
        if budget < 256:
            # 已经几乎溢出；给最少 256 token 的尝试空间，让 provider 自己报错
            # （此时压缩 / overflow 截断应已介入；不能给 0 否则部分 provider 直接拒绝）
            budget = 256

        if cfg_max > 0:
            return min(cfg_max, budget)
        return budget

    def _compute_compress_threshold(self) -> int:
        """
        计算当前轮次的「触发上下文压缩」阈值（token 数）。

        由 ``min`` 三个上限构成，任一收紧都会先触发：

        1. ``memory.max_working_tokens`` —— 配置的绝对上限；
        2. ``CoreProfile.max_context_tokens`` —— 子 Agent / 后台 Core 的额外封顶；
        3. ``ratio * llm_client.context_window`` —— 按当前**活跃模型**上下文窗口的比例。

        引入第 3 项是为了在运行时切换模型（``/model``）后自动适配：例如把 ``max_working_tokens``
        配成 1M 适配大窗口模型，但临时切到 200k 窗口模型时会按比例自动收紧，避免一进 LLM
        就因 prompt 超窗而失败。``ratio`` 取 ``memory.context_window_ratio``；为 ``None``
        或 ``context_window`` 不可用时跳过该项。
        """
        threshold = int(self._working_memory.max_tokens)

        profile = getattr(self, "_core_profile", None)
        mct = getattr(profile, "max_context_tokens", None) if profile else None
        if isinstance(mct, int) and mct > 0:
            threshold = min(threshold, mct)

        ratio = getattr(self._config.memory, "context_window_ratio", None)
        if ratio:
            try:
                cw = int(getattr(self._llm_client, "context_window", 0) or 0)
            except Exception:
                cw = 0
            if cw > 0:
                ratio_threshold = max(int(cw * float(ratio)), 1)
                threshold = min(threshold, ratio_threshold)

        return max(threshold, 1)

    def _estimate_current_context_tokens(self) -> int:
        """估算当前若发起下一次 LLM 请求会占用的 prompt tokens。

        旧的 ``_last_prompt_tokens`` 是上一轮 API 返回的真实 prompt token，只能代表
        「上一次请求」。用户消息、assistant 回复、tool result 写入后，它会变成过期值。
        这里按当前 system + messages + tools schema 估算，更适合 /usage 与压缩检查。
        """
        try:
            from agent_core.kernel_interface.loader import _strip_orphan_tool_messages
            from agent_core.memory.working_memory import (
                estimate_messages_tokens,
                estimate_tokens,
            )

            total = estimate_tokens(self._build_system_prompt())
            total += estimate_messages_tokens(
                _strip_orphan_tool_messages(self._context.get_messages())
            )

            snapshot = self._working_set.build_snapshot(self._tool_registry)
            tools = snapshot.openai_tools
            profile = getattr(self, "_core_profile", None)
            if profile is not None:
                tools = [
                    t
                    for t in tools
                    if profile.is_tool_allowed(t.get("function", {}).get("name", ""))
                ]
            if tools:
                total += estimate_tokens(json.dumps(tools, ensure_ascii=False))
            return int(total)
        except Exception:
            return int(self._working_memory.get_current_tokens())

    def get_token_usage(self) -> dict:
        """
        获取本会话累计的 token 用量。

        Returns:
            包含 prompt_tokens, completion_tokens, total_tokens, call_count，
            DeepSeek 等支持的 prompt_cache_hit_tokens / prompt_cache_miss_tokens 累计，
            cost_yuan、上下文窗口相关字段等。
        """
        out: dict[str, int | float] = dict(self._token_usage)

        # 上下文窗口（context window）相关信息
        max_ctx_tokens = self._llm_client.context_window
        if max_ctx_tokens and max_ctx_tokens > 0:
            # 当前上下文 token 数按“下一次请求 payload”估算（system + messages + tools）。
            # 上一轮 API 返回的 prompt_tokens 也保留为诊断字段；若估算偏小，用真实值兜底。
            estimated_ctx_tokens = self._estimate_current_context_tokens()
            last_prompt_tokens = int(self._last_prompt_tokens or 0)
            current_ctx_tokens = max(estimated_ctx_tokens, last_prompt_tokens)

            remaining_ctx_tokens = max(max_ctx_tokens - current_ctx_tokens, 0)
            out["context_window_max_tokens"] = max_ctx_tokens
            out["context_window_current_tokens"] = current_ctx_tokens
            out["context_window_remaining_tokens"] = remaining_ctx_tokens
            out["context_window_estimated_tokens"] = estimated_ctx_tokens
            if last_prompt_tokens > 0:
                out["context_window_last_prompt_tokens"] = last_prompt_tokens

        cost = compute_cost_from_calls(
            self._usage_calls,
            self._llm_client.model,
            pricing=_pricing_map_for_active_provider(self),
        )
        if cost is not None:
            out["cost_yuan"] = cost
        return out

    def get_turn_count(self) -> int:
        """获取本会话已处理的用户轮次数量"""
        return self._current_turn_id

    def reset_token_usage(self) -> None:
        """重置本会话的 token 用量统计"""
        self._token_usage = _default_token_usage_dict()
        self._usage_calls.clear()

    async def prepare_turn(
        self,
        text: str,
        content_items: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """
        为新一轮处理做准备，返回 turn_id。

        统一了三条执行路径（process_input / KernelScheduler / CoreSessionAdapter）
        中重复的前置处理逻辑，确保行为一致——特别是 memory recall 在所有路径均生效。

        上下文压缩由 run_loop 内部检测并通过 ContextOverflowAction 信号交给 Kernel 处理，
        不再在 prepare_turn 阶段启动并行总结任务。
        """
        await self._sync_external_session_updates()
        self._current_turn_id += 1
        turn_id = self._current_turn_id

        # 运行时 /model 切换后，活跃 provider 的 vision 能力可能变化，
        # 这里每轮都刷新 recognize_image 的可见性。
        self._refresh_recognize_image_visibility()

        if self._memory_enabled and self._recall_policy.should_recall(text):
            recall_result = await asyncio.to_thread(
                self._recall_policy.recall,
                query=text,
                long_term_memory=self._long_term_memory,
                content_memory=self._content_memory,
            )
            self._last_recall_result = recall_result
        else:
            self._last_recall_result = RecallResult()

        # 主模型无 vision 且配置了 vision_provider 时，把本轮 image/video 折叠成文字占位；
        # 原始条目登记到 _last_unseen_media，供 recognize_image 工具按 name 回查。
        effective_text = text
        incoming_items = list(content_items or [])
        immediate_items: List[Dict[str, Any]] = []
        for item in incoming_items:
            if not isinstance(item, dict):
                continue
            if item.get("defer_with_next_user_input"):
                copied = dict(item)
                copied.pop("defer_with_next_user_input", None)
                self._pending_user_multimodal_items.append(copied)
            else:
                immediate_items.append(item)

        # 仅在用户本轮确实给了文本输入时，才把前序缓存媒体并入本轮，
        # 达到“图片和后续文本一起进入下一轮”的交互效果。
        merged_items = list(immediate_items)
        if text.strip() and self._pending_user_multimodal_items:
            merged_items = [*self._pending_user_multimodal_items, *merged_items]
            self._pending_user_multimodal_items.clear()

        effective_media_items: Optional[List[Dict[str, Any]]] = merged_items or None
        if merged_items:
            from agent_core.agent.media_helpers import adapt_content_items_for_provider

            file_preface, adapted_items = adapt_content_items_for_provider(
                merged_items,
                supported_file_mime_types=list(
                    getattr(self._llm_client.capabilities, "file_input_mime_types", ()) or ()
                ),
                enable_native_file_blocks=bool(
                    getattr(self._llm_client, "supports_native_file_blocks", False)
                ),
            )
            if file_preface:
                effective_text = f"{effective_text}\n\n{file_preface}" if effective_text else file_preface
            effective_media_items = adapted_items or None

        try:
            caps_vision = bool(self._llm_client.capabilities.vision)
            has_vision_provider = bool(self._llm_client.vision_provider_name)
            should_downgrade = (not caps_vision) and has_vision_provider
        except Exception:
            should_downgrade = False
        if should_downgrade and effective_media_items:
            from agent_core.agent.media_helpers import downgrade_user_media_to_text

            placeholder_text, kept_items = downgrade_user_media_to_text(
                effective_media_items, unseen_media=self._last_unseen_media
            )
            if placeholder_text:
                effective_text = (
                    f"{effective_text}\n\n{placeholder_text}"
                    if effective_text
                    else placeholder_text
                )
            effective_media_items = kept_items or None

        if _should_attach_user_time_stamp(effective_text, effective_media_items):
            effective_text = apply_user_message_time_prefix(
                effective_text, self._timezone
            )

        self._context.add_user_message(
            effective_text, media_items=effective_media_items
        )
        # context 已变化，上一轮 API 返回的 prompt_tokens 不再代表当前窗口。
        self._last_prompt_tokens = None
        self._outgoing_attachments.clear()
        if self._session_logger:
            self._session_logger.on_user_message(turn_id, text)
        if self._memory_enabled:
            msg_id = self._require_chat_history_db().write_message(
                session_id=self._session_id,
                role="user",
                content=text,
                source=self._source,
            )
            self._last_history_id = max(self._last_history_id, int(msg_id))

        return turn_id

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

        turn_id = await self.prepare_turn(user_input, content_items)

        hooks = AgentHooks(
            on_assistant_delta=on_stream_delta,
            on_reasoning_delta=on_reasoning_delta,
            on_trace_event=on_trace_event,
        )
        kernel = AgentKernel(tool_registry=self._tool_registry)
        run_result = None
        try:
            run_result = await kernel.run(self, turn_id=turn_id, hooks=hooks)
        finally:
            await self._finalize_turn(run_result)

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
            ReturnAction,
            ToolCallAction,
            ToolResultEvent,
            InternalLoader,
            ContextOverflowAction,
        )

        loader = InternalLoader()
        iteration = 0

        while iteration < self._max_iterations:
            iteration += 1
            previous_messages = self._context.get_messages()

            try:
                # ── 上下文压缩检查（信号机制：Core 检测 → Kernel 执行）──────
                current_tokens = self._estimate_current_context_tokens()
                compress_threshold = self._compute_compress_threshold()
                if current_tokens >= compress_threshold:
                    _ = yield ContextOverflowAction(
                        current_tokens=current_tokens,
                        threshold_tokens=compress_threshold,
                        session_id=self._session_id,
                    )
                    # 摘要由 Kernel 写入 context.messages；此处不再写 running_summary

                missing_reasoning_retries = 0
                while True:
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
                            system_prompt=payload.system
                            if self._session_logger.enable_detailed_log
                            else None,
                            messages=payload.messages
                            if self._session_logger.enable_detailed_log
                            else None,
                        )

                    # Trace 事件
                    await self._emit_trace(
                        hooks,
                        {
                            "type": "llm_request",
                            "turn_id": turn_id,
                            "iteration": iteration,
                            "tool_count": len(payload.tools),
                            "retry_missing_reasoning": missing_reasoning_retries,
                        },
                    )

                    # 自适应 completion 预算：把 max_tokens 收到
                    # ``min(配置 max_tokens, context_window − estimated_prompt − safety)``，
                    # 防止常量 max_tokens（例如 65536）+ prompt 一起把窗口顶爆。
                    effective_max_tokens = self._compute_effective_max_tokens(payload)

                    # ── AgentCore 直接调用 LLM（CPU 自旋，无 Kernel 中介）───
                    response = await self._llm_client.chat_with_tools(
                        system_message=payload.system,
                        messages=payload.messages,
                        tools=payload.tools,
                        tool_choice="auto",
                        on_content_delta=hooks.on_assistant_delta if hooks else None,
                        on_reasoning_delta=hooks.on_reasoning_delta if hooks else None,
                        max_tokens_override=effective_max_tokens,
                    )

                    if self._session_logger:
                        self._session_logger.on_llm_response(
                            turn_id, iteration, response
                        )

                    # 累计 token 用量（AgentCore 内部状态，Kernel 在回收时读取）
                    if response.usage:
                        pt, ct = (
                            response.usage.prompt_tokens,
                            response.usage.completion_tokens,
                        )
                        self._token_usage["prompt_tokens"] += pt
                        self._token_usage["completion_tokens"] += ct
                        self._token_usage["total_tokens"] += pt + ct
                        self._token_usage["call_count"] += 1
                        self._token_usage["prompt_cache_hit_tokens"] += int(
                            response.usage.prompt_cache_hit_tokens or 0
                        )
                        self._token_usage["prompt_cache_miss_tokens"] += int(
                            response.usage.prompt_cache_miss_tokens or 0
                        )
                        self._usage_calls.append(
                            (
                                pt,
                                ct,
                                int(response.usage.prompt_cache_hit_tokens or 0),
                                int(response.usage.prompt_cache_miss_tokens or 0),
                            )
                        )
                        self._last_prompt_tokens = pt

                    if self._should_synthesize_empty_tool_reasoning(response):
                        response.reasoning_content = ""
                        tool_names = [tc.name for tc in response.tool_calls]
                        logger.warning(
                            "DeepSeek thinking tool response missing reasoning_content; "
                            "synthesizing empty field: session=%s turn=%s iteration=%s tools=%s",
                            self._session_id,
                            turn_id,
                            iteration,
                            tool_names,
                        )
                        # region agent log
                        _agent_debug_log(
                            "H1-H5",
                            "src/agent_core/agent/agent.py:run_loop:synthesize_empty_tool_reasoning",
                            "synthesized empty reasoning_content for DeepSeek tool-call response",
                            {
                                "session_id": self._session_id,
                                "turn_id": turn_id,
                                "iteration": iteration,
                                "tool_names": tool_names,
                                "tool_call_count": len(response.tool_calls),
                            },
                        )
                        # endregion
                        break

                    if not self._should_retry_missing_tool_reasoning(response):
                        break

                    # region agent log
                    _prov = None
                    try:
                        _prov = self._llm_client._active_provider()
                    except Exception:
                        _prov = None
                    _u = getattr(response, "usage", None)
                    _agent_debug_log(
                        "H3",
                        "src/agent_core/agent/agent.py:run_loop:missing_tool_reasoning_before_retry",
                        "missing reasoning on tool_calls; will retry or fatal",
                        {
                            "session_id": self._session_id,
                            "turn_id": turn_id,
                            "iteration": iteration,
                            "next_retry_count": missing_reasoning_retries + 1,
                            "looks_deepseek_name_model_base": (
                                self._looks_like_deepseek_api(_prov) if _prov else False
                            ),
                            "would_synth_ds_cache_split": (
                                self._looks_like_deepseek_usage_cache_split(response)
                                if _prov
                                else False
                            ),
                            "provider_cls": type(_prov).__name__ if _prov else None,
                            "provider_key": str(getattr(_prov, "name", "") or ""),
                            "cache_hit": getattr(_u, "prompt_cache_hit_tokens", None)
                            if _u
                            else None,
                            "cache_miss": getattr(_u, "prompt_cache_miss_tokens", None)
                            if _u
                            else None,
                        },
                    )
                    # endregion

                    missing_reasoning_retries += 1
                    tool_names = [tc.name for tc in response.tool_calls]
                    msg = (
                        "模型返回了包含 tool_calls 但缺少 reasoning_content 的不完整响应，"
                        "已丢弃本次响应并重试"
                    )
                    logger.warning(
                        "%s: session=%s turn=%s iteration=%s retry=%s tools=%s",
                        msg,
                        self._session_id,
                        turn_id,
                        iteration,
                        missing_reasoning_retries,
                        tool_names,
                    )
                    await self._emit_trace(
                        hooks,
                        {
                            "type": "llm_incomplete_tool_reasoning",
                            "turn_id": turn_id,
                            "iteration": iteration,
                            "retry": missing_reasoning_retries,
                            "tool_names": tool_names,
                            "message": msg,
                        },
                    )
                    if missing_reasoning_retries <= _MISSING_TOOL_REASONING_RETRY_LIMIT:
                        continue

                    fatal = (
                        "模型返回了包含工具调用但缺少 reasoning_content 的不完整响应，"
                        "已终止本轮以避免污染会话上下文。"
                    )
                    if self._session_logger:
                        self._session_logger.on_assistant_message(turn_id, fatal)
                    yield ReturnAction(
                        message=fatal,
                        status="error",
                        attachments=list(self._outgoing_attachments),
                    )
                    return

                # 子 Agent token 上限：超限则强制结束，防止卡住
                profile = getattr(self, "_core_profile", None)
                if (
                    profile is not None
                    and getattr(profile, "mode", None) == "sub"
                    and getattr(profile, "max_total_tokens", None) is not None
                ):
                    limit = profile.max_total_tokens
                    if self._token_usage["total_tokens"] >= limit:
                        reason = (
                            f"子任务已达到 token 上限（{self._token_usage['total_tokens']} >= {limit}），已强制结束"
                        )
                        log_path = (
                            getattr(self._session_logger, "file_path", None)
                            if self._session_logger
                            else None
                        )
                        log_dir = getattr(
                            getattr(self._config, "logging", None),
                            "session_log_dir",
                            "./logs/sessions",
                        )
                        subagent_id = (
                            (self._session_id or "").replace("sub:", "", 1)
                            if (self._session_id or "").startswith("sub:")
                            else (self._session_id or "")
                        )
                        overflow_msg = _format_subagent_limit_msg(
                            reason=reason,
                            subagent_id=subagent_id,
                            log_path=log_path,
                            log_dir=log_dir,
                            limit_type="subagent_max_tokens",
                        )
                        if self._session_logger:
                            self._session_logger.on_assistant_message(
                                turn_id, overflow_msg
                            )
                        yield ReturnAction(
                            message=overflow_msg,
                            status="overflow",
                            attachments=list(self._outgoing_attachments),
                        )
                        return

                # ── 处理工具调用 ─────────────────────────────────────────
                if response.tool_calls:
                    # region agent log
                    _agent_debug_log(
                        "H2-H3",
                        "src/agent_core/agent/agent.py:run_loop:before_add_tool_assistant",
                        "about to store assistant tool-call response in context",
                        {
                            "session_id": self._session_id,
                            "turn_id": turn_id,
                            "iteration": iteration,
                            "tool_call_count": len(response.tool_calls),
                            "reasoning_present": response.reasoning_content is not None,
                            "reasoning_nonempty": bool((response.reasoning_content or "").strip()),
                            "content_present": bool(response.content),
                        },
                    )
                    # endregion
                    self._add_assistant_message_with_tool_calls(response)
                    self._last_prompt_tokens = None

                    for tool_call in response.tool_calls:
                        # Trace 事件（发出调用前记录）
                        await self._emit_trace(
                            hooks,
                            {
                                "type": "tool_call",
                                "turn_id": turn_id,
                                "iteration": iteration,
                                "tool_call_id": tool_call.id,
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                            },
                        )
                        if self._session_logger:
                            self._session_logger.on_tool_call(
                                turn_id,
                                iteration,
                                ToolCall(
                                    id=tool_call.id,
                                    name=tool_call.name,
                                    arguments=tool_call.arguments or {},
                                    extra_content=tool_call.extra_content,
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

                        assert isinstance(tool_event, ToolResultEvent), (
                            f"run_loop: expected ToolResultEvent, got {type(tool_event)}"
                        )
                        result = tool_event.result

                        # Trace 事件（收到结果后记录，含耗时）
                        tr_payload: Dict[str, Any] = {
                            "type": "tool_result",
                            "turn_id": turn_id,
                            "iteration": iteration,
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "success": result.success,
                            "message": result.message,
                            "duration_ms": duration_ms,
                            "error": result.error,
                        }
                        data_preview = result.data_preview_str(6000)
                        if data_preview:
                            tr_payload["data_preview"] = data_preview
                        await self._emit_trace(hooks, tr_payload)
                        if self._session_logger:
                            self._session_logger.on_tool_result(
                                turn_id, iteration, tool_call.id, result, duration_ms
                            )

                        # 媒体与待发送附件用「原始 result」，避免截断丢失 data 中的
                        # 资源指针（图片/视频路径等）。
                        self._queue_media_for_next_call(result)
                        self._collect_outgoing_attachment(result)

                        # 入场截断：单条 tool result 超阈值时落盘到工作区
                        # .tool_results/，messages 与 chat_history.db 中只留 head + 标记，
                        # 防止单次 web_search/file_read 等撑爆模型上下文窗口。
                        store_result = self._maybe_offload_tool_result_for_context(
                            result,
                            tool_name=tool_call.name,
                            tool_call_id=tool_call.id,
                            turn_id=turn_id,
                            iteration=iteration,
                        )

                        self._context.add_tool_result(tool_call.id, store_result)
                        self._last_prompt_tokens = None

                        if self._memory_enabled:
                            msg_id = self._require_chat_history_db().write_message(
                                session_id=self._session_id,
                                role="tool",
                                content=store_result.to_json(),
                                tool_name=tool_call.name,
                                source=self._source,
                            )
                            self._last_history_id = max(
                                self._last_history_id, int(msg_id)
                            )

                    continue

                # ── 最终响应，先检查上下文溢出再 ReturnAction ─────────────
                if response.content:
                    if self._should_synthesize_empty_final_reasoning(response):
                        response.reasoning_content = ""
                        logger.warning(
                            "DeepSeek thinking final message missing reasoning_content; "
                            "synthesizing empty field: session=%s turn=%s iteration=%s",
                            self._session_id,
                            turn_id,
                            iteration,
                        )
                        # region agent log
                        _agent_debug_log(
                            "H5",
                            "src/agent_core/agent/agent.py:run_loop:synthesize_empty_final_reasoning",
                            "synthesized empty reasoning_content for DeepSeek final assistant message",
                            {
                                "session_id": self._session_id,
                                "turn_id": turn_id,
                                "iteration": iteration,
                            },
                        )
                        # endregion
                    # region agent log
                    _agent_debug_log(
                        "H3",
                        "src/agent_core/agent/agent.py:run_loop:before_add_final_assistant",
                        "about to store final assistant response in context",
                        {
                            "session_id": self._session_id,
                            "turn_id": turn_id,
                            "iteration": iteration,
                            "reasoning_present": response.reasoning_content is not None,
                            "reasoning_nonempty": bool((response.reasoning_content or "").strip()),
                            "content_present": bool(response.content),
                        },
                    )
                    # endregion
                    self._context.add_assistant_message(
                        content=response.content,
                        reasoning_content=response.reasoning_content,
                        anthropic_message_content=getattr(
                            response, "anthropic_message_content", None
                        ),
                    )
                    self._last_prompt_tokens = None
                    if self._session_logger:
                        self._session_logger.on_assistant_message(
                            turn_id, response.content
                        )
                    if self._memory_enabled:
                        msg_id = self._require_chat_history_db().write_message(
                            session_id=self._session_id,
                            role="assistant",
                            content=response.content,
                            source=self._source,
                        )
                        self._last_history_id = max(self._last_history_id, int(msg_id))

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
        overflow_msg = (
            "抱歉，处理您的请求时超出了最大迭代次数。请简化您的问题或稍后重试。"
        )
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
        run_result: "Optional[AgentRunResult]",
    ) -> None:
        """
        本轮后处理：写入检查点。

        由 process_input() 和 KernelScheduler._run_and_route() 在
        AgentKernel.run() 完成后调用。

        检查点存 last_active_at（本 turn 结束时间）；是否过期由 kernel 下次启动时
        用「kernel 关闭时间戳 - last_active_at」计算 elapsed 判断。
        """
        # 每轮结束后写入检查点（last_active_at = now）；过期判断在 kernel 启动时用关闭时间戳计算
        if self._checkpoint_manager is not None:
            try:
                profile = getattr(self, "_core_profile", None)
                ttl = float(
                    getattr(profile, "session_expired_seconds", None)
                    or getattr(self._config.agent, "session_expired_seconds", 1800)
                )
                self._checkpoint_manager.write(
                    CoreCheckpoint(
                        session_id=self._session_id,
                        owner_id=self._user_id,
                        source=self._source,
                        running_summary=self._working_memory.running_summary,
                        recent_messages=list(self._context.get_messages()),
                        last_active_at=time.time(),
                        remaining_ttl_seconds=ttl,
                        turn_count=self._current_turn_id,
                        last_history_id=self._last_history_id,
                        token_usage=dict(self._token_usage),
                        compression_round=self._working_memory.compression_round,
                        core_profile=core_profile_to_checkpoint_dict(profile),
                    )
                )
            except Exception as exc:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "AgentCore: checkpoint write failed: %s", exc
                )

    def flush_checkpoint_for_shutdown(self) -> None:
        """Daemon / Kernel 正常关闭前刷新检查点，将 ``last_active_at`` 更新为当前时刻。

        每轮 turn 结束时才会写入 checkpoint；若会话长时间空闲但仍未超过运行时 TTL，
        磁盘上的 ``last_active_at`` 会停留在「上一轮结束时刻」。下次启动时
        ``restore_from_checkpoints`` 用 ``shutdown_at - last_active_at`` 计算 elapsed，
        会把「空闲时长」误判为停机时间，导致尚未过期的会话无法恢复。

        在 ``evict(..., shutdown=True)`` 路径调用本方法，使暂停语义成立：停机前后不把
        空闲等待算进恢复时的 TTL 折算。
        """
        if self._checkpoint_manager is None:
            return
        try:
            profile = getattr(self, "_core_profile", None)
            ttl = float(
                getattr(profile, "session_expired_seconds", None)
                or getattr(self._config.agent, "session_expired_seconds", 1800)
            )
            self._checkpoint_manager.write(
                CoreCheckpoint(
                    session_id=self._session_id,
                    owner_id=self._user_id,
                    source=self._source,
                    running_summary=self._working_memory.running_summary,
                    recent_messages=list(self._context.get_messages()),
                    last_active_at=time.time(),
                    remaining_ttl_seconds=ttl,
                    turn_count=self._current_turn_id,
                    last_history_id=self._last_history_id,
                    token_usage=dict(self._token_usage),
                    compression_round=self._working_memory.compression_round,
                    core_profile=core_profile_to_checkpoint_dict(profile),
                )
            )
        except Exception as exc:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "AgentCore: shutdown checkpoint flush failed: %s", exc
            )

    def _build_system_prompt(self) -> str:
        """
        构建系统提示。

        由 ``build_agent_system_prompt`` 组装：``get_recipe(_source)`` 决定 identity/overlay/Workspace 段；
        记忆上下文与可选自动化摘要见 ``prompt_builder``。
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
            if isinstance(tc.arguments, str):
                # 确保 arguments 是合法 JSON 字符串，否则 API 会拒绝（400）
                try:
                    json.loads(tc.arguments)
                    args_str = tc.arguments
                except (json.JSONDecodeError, ValueError):
                    args_str = json.dumps({}, ensure_ascii=False)
            else:
                args_str = json.dumps(tc.arguments, ensure_ascii=False)
            entry: Dict[str, Any] = {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": args_str,
                },
            }
            if tc.extra_content:
                entry["extra_content"] = tc.extra_content
            tool_calls.append(entry)

        self._context.add_assistant_message(
            content=response.content,
            tool_calls=tool_calls,
            reasoning_content=response.reasoning_content,
            anthropic_message_content=getattr(
                response, "anthropic_message_content", None
            ),
        )

    async def _execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        """
        执行工具调用。

        ⚠️  注意：此方法在主 Agent 循环中**不被调用**。
        实际执行路径为：run_loop() → yield ToolCallAction → AgentKernel.run()
        → agent_registry.execute()（Kernel 直接调用，不经过本方法）。

        本方法仅在单元测试中用于直接测试参数解析和工具执行逻辑，
        不应在生产代码中调用。可见性检查和 __execution_context__ 注入
        均已在 AgentKernel 侧实现。

        Args:
            tool_call: 工具调用

        Returns:
            工具执行结果
        """
        if self._current_visible_tools and tool_call.name not in self._current_visible_tools:
            return ToolResult(
                success=False,
                error="TOOL_NOT_VISIBLE",
                message=f"工具 '{tool_call.name}' 当前不在可见工作集中",
            )

        # 解析参数（流式解析失败时 arguments 可能为原始 JSON 字符串）
        if isinstance(tool_call.arguments, str):
            try:
                kwargs = json.loads(tool_call.arguments)
            except json.JSONDecodeError:
                raw_preview = tool_call.arguments
                if len(raw_preview) > 500:
                    raw_preview = raw_preview[:500] + "...(已截断)"
                return ToolResult(
                    success=False,
                    error="INVALID_ARGUMENTS",
                    message=f"工具参数格式错误（可能为流式输出截断导致 JSON 不完整）: {raw_preview}",
                )
        else:
            kwargs = tool_call.arguments

        # 注入执行上下文（供 bash/file_tools 等做来源与权限鉴权）
        kwargs = dict(kwargs)
        profile = getattr(self, "_core_profile", None)
        kwargs["__execution_context__"] = {
            "profile_mode": getattr(profile, "mode", "full") if profile is not None else "full",
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
            "source": self._source,
            "user_id": self._user_id,
        }

        # 执行工具
        return await self._tool_registry.execute(tool_call.name, **kwargs)

    def _queue_media_for_next_call(self, result: ToolResult) -> None:
        """将工具结果中声明的媒体挂载到下一次 LLM 调用。"""
        prof = getattr(self, "_core_profile", None)
        media_ctx = {
            "source": self._source,
            "user_id": self._user_id,
            "bash_workspace_admin": bool(getattr(prof, "bash_workspace_admin", False))
            if prof is not None
            else False,
        }

        def _resolver(p: str):
            return resolve_media_to_content_item(
                p, config=self._config, exec_ctx=media_ctx
            )

        queue_media_for_next_call(
            result,
            self._pending_multimodal_items,
            media_resolver=_resolver,
        )

    def _collect_outgoing_attachment(self, result: ToolResult) -> None:
        """将工具结果中声明的「随回复发给用户的附件」加入本轮待发送列表。"""
        collect_outgoing_attachment(result, self._outgoing_attachments)

    def _maybe_offload_tool_result_for_context(
        self,
        result: ToolResult,
        *,
        tool_name: str,
        tool_call_id: str,
        turn_id: int = 0,
        iteration: int = 0,
    ) -> ToolResult:
        """
        包装 :func:`tool_result_overflow.maybe_offload_tool_result`：

        若 ``memory.max_tool_result_tokens`` 为 None/0 或当前 result 不超阈值，
        原样返回；否则把完整 JSON 落盘到工作区 ``.tool_results/``，返回截断后的
        新 ``ToolResult``。

        触发时会通过 ``session_logger`` 记一条 ``tool_overflow`` 审计事件，
        便于事后定位「为什么这条 tool result 在 messages 里只剩 head」。
        """
        from agent_core.agent.tool_result_overflow import (
            maybe_offload_tool_result,
            resolve_overflow_dirs,
        )

        mem_cfg = self._config.memory
        max_tokens = getattr(mem_cfg, "max_tool_result_tokens", None)
        if not max_tokens or max_tokens <= 0:
            return result

        cmd_cfg = self._config.command_tools
        overflow_dir_name = (
            getattr(mem_cfg, "tool_result_overflow_dir", None) or ".tool_results"
        )
        try:
            workspace_dir, is_admin, admin_dir = resolve_overflow_dirs(
                cmd_cfg=cmd_cfg,
                user_id=self._user_id,
                source=self._source,
                profile=getattr(self, "_core_profile", None),
                overflow_dir_name=overflow_dir_name,
            )
        except Exception as exc:  # pragma: no cover —— workspace_paths 极少抛
            logger.warning(
                "_maybe_offload_tool_result_for_context: resolve dirs failed: %s; skip",
                exc,
            )
            return result

        new_result, outcome = maybe_offload_tool_result(
            result,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            workspace_dir=workspace_dir,
            max_tokens=int(max_tokens),
            overflow_dir_name=overflow_dir_name,
            is_workspace_admin=is_admin,
            admin_overflow_dir=admin_dir,
        )

        if outcome.triggered and self._session_logger is not None:
            try:
                self._session_logger.on_tool_overflow(
                    turn_id=turn_id,
                    iteration=iteration,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    original_tokens=outcome.original_tokens,
                    kept_tokens=outcome.kept_tokens,
                    max_tokens=int(max_tokens),
                    overflow_path=str(outcome.overflow_path)
                    if outcome.overflow_path
                    else "",
                    display_path=outcome.display_path,
                )
            except Exception:  # session_logger 写失败不应影响主路径
                pass

        return new_result

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
        vision_supported = True
        try:
            caps_vision = bool(self._llm_client.capabilities.vision)
            has_vision_provider = bool(self._llm_client.vision_provider_name)
            # 只有当主模型无 vision 且我们能降级到 recognize_image 工具时，才启用文字占位；
            # 否则保留原始多模态挂载（历史行为），让上游按实际能力报错或正常处理。
            vision_supported = caps_vision or not has_vision_provider
        except Exception:
            vision_supported = True
        return append_pending_multimodal_messages(
            messages,
            self._pending_multimodal_items,
            vision_supported=vision_supported,
            unseen_media=self._last_unseen_media,
        )

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
        self._require_long_term_memory().add_recent_topic(
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

    async def activate_session(
        self, session_id: str, replay_messages_limit: Optional[int] = 0
    ) -> None:
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
        self._last_recall_result = RecallResult()
        self._pending_multimodal_items.clear()
        self._last_history_id = 0
        self.reset_token_usage()
        self._working_memory = WorkingMemory(
            context=self._context,
            max_tokens=self._config.memory.max_working_tokens,
        )

        if not self._memory_enabled:
            return

        history = self._require_chat_history_db().get_session_messages(sid)
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
        # region agent log
        _agent_debug_log(
            "H4",
            "src/agent_core/agent/agent.py:activate_session",
            "session replay from ChatHistoryDB completed",
            {
                "session_id": self._session_id,
                "replay_rows": len(replay_rows),
                "history_rows": len(history),
                "stats": _debug_context_reasoning_stats(self._context.get_messages()),
            },
        )
        # endregion
        self._current_turn_id = sum(1 for r in replay_rows if r.get("role") == "user")
        self._last_history_id = max(int(r.get("id", 0)) for r in history)
        if replay_rows:
            first_ts = replay_rows[0].get("timestamp")
            if isinstance(first_ts, str) and first_ts.strip():
                self._session_start_time = first_ts

    def restore_from_checkpoint(self, checkpoint: CoreCheckpoint) -> None:
        """
        从检查点恢复会话状态，跳过 ChatHistoryDB 全量重放。

        恢复内容：
        - ConversationContext.messages（压缩后的上下文窗口）
        - WorkingMemory（running_summary / compression_round 等元数据，主对话以 messages 为准）
        - session_id、turn_count、last_history_id、token_usage

        由 CorePool._load() 在读取到有效检查点时调用，
        替代 activate_session() 的 ChatHistoryDB 重放路径。
        """
        self._session_id = checkpoint.session_id
        self._current_turn_id = checkpoint.turn_count
        self._last_history_id = checkpoint.last_history_id
        self._token_usage = {
            **_default_token_usage_dict(),
            **dict(checkpoint.token_usage),
        }
        self._last_prompt_tokens = None
        self._last_recall_result = RecallResult()
        self._pending_multimodal_items.clear()

        self._context.clear()
        for msg in checkpoint.recent_messages:
            self._context.messages.append(dict(msg))
        # region agent log
        _agent_debug_log(
            "H4",
            "src/agent_core/agent/agent.py:restore_from_checkpoint",
            "session restored from checkpoint",
            {
                "session_id": self._session_id,
                "checkpoint_messages": len(checkpoint.recent_messages),
                "stats": _debug_context_reasoning_stats(self._context.get_messages()),
            },
        )
        # endregion

        self._working_memory = WorkingMemory(
            context=self._context,
            max_tokens=self._config.memory.max_working_tokens,
        )
        self._working_memory.running_summary = checkpoint.running_summary
        self._working_memory.compression_round = getattr(
            checkpoint, "compression_round", 0
        )

    async def _sync_external_session_updates(self) -> None:
        """同步其他终端在同一 session 里新增的 user/assistant 消息。"""
        if not self._memory_enabled:
            return
        new_rows = self._require_chat_history_db().get_session_messages_after(
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
        # region agent log
        _agent_debug_log(
            "H4",
            "src/agent_core/agent/agent.py:_sync_external_session_updates",
            "external ChatHistoryDB rows synced into active context",
            {
                "session_id": self._session_id,
                "new_rows": len(new_rows),
                "stats": _debug_context_reasoning_stats(self._context.get_messages()),
            },
        )
        # endregion
        # 有外部新增时，强制让本轮阈值判断基于当前上下文重估，确保压缩及时触发。
        self._last_prompt_tokens = None
        self._last_history_id = max(
            self._last_history_id, max(int(r.get("id", 0)) for r in new_rows)
        )

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
        )

    async def close_mcp_only(self) -> None:
        """仅关闭 MCP 连接。供“连接所在任务”在退出时调用，保证 anyio cancel scope 同任务 enter/exit。"""
        if self._mcp_manager is None:
            return
        await self._mcp_manager.close()
        self._mcp_manager = None
        self._mcp_connected = False
        self._mcp_closed_by_owner = True

    async def close(self) -> None:
        """关闭 Agent，释放资源"""
        if self._bash is not None:
            try:
                await self._bash.close(
                    write_snapshot=self._config.command_tools.snapshot_enabled,
                )
            except Exception:
                pass
            self._bash = None
        await self._llm_client.close()
        if self._summary_llm_client is not self._llm_client:
            await self._summary_llm_client.close()
        if self._mcp_manager and not self._mcp_closed_by_owner:
            await self._mcp_manager.close()
            self._mcp_manager = None
            self._mcp_connected = False
        elif self._mcp_closed_by_owner:
            self._mcp_manager = None
            self._mcp_connected = False
        if self._memory_enabled and self._chat_history_db is not None:
            self._chat_history_db.close()

    def _require_chat_history_db(self) -> ChatHistoryDB:
        """
        返回非可选的 ChatHistoryDB。

        仅在已确认记忆系统启用且 ChatHistoryDB 已初始化的路径中调用。
        """
        if self._chat_history_db is None:
            raise RuntimeError("ChatHistoryDB is not initialized")
        return self._chat_history_db

    def _require_long_term_memory(self) -> LongTermMemory:
        """
        返回非可选的 LongTermMemory。

        仅在已确认记忆系统启用且 LongTermMemory 已初始化的路径中调用。
        """
        if self._long_term_memory is None:
            raise RuntimeError("LongTermMemory is not initialized")
        return self._long_term_memory

    async def __aenter__(self) -> "AgentCore":
        """异步上下文管理器入口"""
        # 启动持久化 Bash 会话并注册 BashTool
        cmd_cfg = self._config.command_tools
        if cmd_cfg.enabled and cmd_cfg.allow_run:
            from agent_core.bash_runtime import BashRuntime, BashRuntimeConfig
            from agent_core.bash_security import BashSecurity
            from agent_core.tools.bash_tool import BashTool

            from agent_core.agent.memory_paths import resolve_memory_owner_paths
            from agent_core.agent.workspace_paths import (
                build_bash_admin_bootstrap_init,
                build_bash_workspace_guard_init,
                ensure_workspace_owner_layout,
                is_bash_workspace_admin,
                migrate_legacy_workspace_and_memory_to_home,
                merged_bash_write_root_paths,
                resolve_bash_working_dir,
                resolve_project_root,
                resolve_workspace_tmp_dir,
            )
            from agent_core.bash_os_user import (
                chown_tree_to_user,
                minimal_subprocess_env_for_runuser,
                provision_system_user,
                resolve_os_user_home,
                resolve_bash_run_as_user,
            )

            profile = self._core_profile
            ensure_workspace_owner_layout(
                cmd_cfg,
                self._user_id,
                source=self._source,
                app_config=self._config,
            )
            bash_cwd = resolve_bash_working_dir(
                cmd_cfg, self._user_id, source=self._source, profile=profile
            )
            ws_restricted = cmd_cfg.workspace_isolation_enabled and not is_bash_workspace_admin(
                cmd_cfg, self._source, self._user_id, profile
            )
            posix_run_as, _os_reason = resolve_bash_run_as_user(
                cmd_cfg,
                source=self._source,
                user_id=self._user_id,
                ws_restricted=ws_restricted,
                profile=profile,
            )
            os_user_bash = bool(posix_run_as)
            jail_cd = not (os_user_bash and ws_restricted)
            if os_user_bash and ws_restricted:
                posix_home = resolve_os_user_home(cmd_cfg, posix_run_as)
                if getattr(cmd_cfg, "bash_os_auto_provision_users", True):
                    provision_system_user(
                        posix_run_as,
                        system=True,
                        comment=f"macchiato {self._source}-{self._user_id}",
                        home_dir=posix_home,
                    )
                migrate_legacy_workspace_and_memory_to_home(
                    cmd_cfg,
                    self._config.memory if self._memory_enabled else None,
                    source=self._source,
                    user_id=self._user_id,
                )
                ch_paths = [
                    Path(bash_cwd).resolve(),
                    Path(
                        resolve_workspace_tmp_dir(
                            cmd_cfg, self._user_id, source=self._source
                        )
                    ).resolve(),
                ]
                if self._memory_enabled:
                    mp0 = resolve_memory_owner_paths(
                        self._config.memory,
                        self._user_id,
                        config=self._config,
                        source=self._source,
                    )
                    ch_paths.append(
                        Path(mp0["chat_history_db_path"]).parent.resolve()
                    )
                chown_tree_to_user(ch_paths, posix_run_as)
            mem_lt: Optional[str] = None
            mem_owner_dir: Optional[str] = None
            if self._memory_enabled:
                mp = resolve_memory_owner_paths(
                    self._config.memory,
                    self._user_id,
                    config=self._config,
                    source=self._source,
                )
                mem_lt = mp["long_term_dir"]
                mem_owner_dir = str(Path(mp["chat_history_db_path"]).parent)
            if ws_restricted:
                guard_init = build_bash_workspace_guard_init(
                    str(Path(bash_cwd).resolve()),
                    project_root=str(resolve_project_root().resolve()),
                    memory_long_term_dir=mem_lt,
                    memory_owner_dir=mem_owner_dir,
                    extra_real_home_path_suffixes=list(
                        cmd_cfg.bash_real_home_path_suffixes or []
                    ),
                    jail_cd=jail_cd,
                )
            elif os_user_bash:
                guard_init = build_bash_admin_bootstrap_init(
                    str(Path(bash_cwd).resolve()),
                    project_root=str(resolve_project_root().resolve()),
                    memory_long_term_dir=mem_lt,
                    memory_owner_dir=mem_owner_dir,
                    extra_real_home_path_suffixes=list(
                        cmd_cfg.bash_real_home_path_suffixes or []
                    ),
                )
            else:
                guard_init = []
            jail_root = (
                str(Path(bash_cwd).resolve()) if ws_restricted else None
            )
            tmp_root = (
                resolve_workspace_tmp_dir(cmd_cfg, self._user_id, source=self._source)
                if ws_restricted
                else None
            )
            extra_write_roots = (
                merged_bash_write_root_paths(
                    cmd_cfg,
                    self._source,
                    self._user_id,
                    app_config=self._config,
                )
                if ws_restricted
                else []
            )
            init_cmds = guard_init + list(cmd_cfg.init_commands or [])
            sub_env = None
            if posix_run_as:
                session_home = Path(bash_cwd).resolve()
                sub_env = minimal_subprocess_env_for_runuser(
                    cwd=Path(bash_cwd).resolve(),
                    project_root=resolve_project_root().resolve(),
                    macchiato_real_home=Path.home().resolve(),
                    home_for_session=session_home,
                )
            rt_config = BashRuntimeConfig(
                shell_path=cmd_cfg.shell_path,
                base_dir=bash_cwd,
                default_timeout_seconds=cmd_cfg.default_timeout_seconds,
                max_timeout_seconds=cmd_cfg.max_timeout_seconds,
                default_output_limit=cmd_cfg.default_output_limit,
                max_output_limit=cmd_cfg.max_output_limit,
                init_commands=init_cmds,
                snapshot_enabled=cmd_cfg.snapshot_enabled,
                snapshot_dir=cmd_cfg.snapshot_dir,
                run_as_user=posix_run_as,
                runuser_path=cmd_cfg.bash_runuser_path,
                subprocess_env=sub_env,
            )
            self._bash = BashRuntime(config=rt_config)
            await self._bash.start()
            self._bash_security = BashSecurity(
                restricted_whitelist=list(cmd_cfg.subagent_command_whitelist or []),
                allow_run_for_restricted=cmd_cfg.allow_run_for_subagent,
                workspace_jail_root=jail_root,
                workspace_tmp_root=tmp_root,
                workspace_extra_write_roots=extra_write_roots if ws_restricted else None,
            )
            if not self._tool_registry.has("bash"):
                self._tool_registry.register(
                    BashTool(bash=self._bash, security=self._bash_security)
                )

        if self._config.mcp.enabled and not self._mcp_connected:
            runtime_servers = self._build_runtime_mcp_servers(self._config.mcp.servers)
            # 不写回全局共享 Config 单例（self._config.mcp.servers），
            # 用 model_copy 创建副本，避免多 AgentCore 并发时服务器列表重复追加。
            try:
                runtime_mcp_cfg = self._config.mcp.model_copy(
                    update={"servers": runtime_servers}
                )
            except AttributeError:
                import copy as _copy_mod
                runtime_mcp_cfg = _copy_mod.copy(self._config.mcp)
                runtime_mcp_cfg.servers = runtime_servers
            self._mcp_manager = MCPClientManager(runtime_mcp_cfg)
            if not self._defer_mcp_connect:
                await self._mcp_manager.connect()
                proxy_tools = self._mcp_manager.get_proxy_tools()
                self._apply_mcp_proxy_tools(proxy_tools)
                self._mcp_connected = True
        return self

    async def ensure_mcp_connected(self) -> bool:
        """若启用了 MCP 且为延迟连接，则执行连接并更新工具注册表。用于 daemon 启动后再连 MCP。"""
        if (
            not self._config.mcp.enabled
            or self._mcp_connected
            or self._mcp_manager is None
        ):
            return self._mcp_connected
        await self._mcp_manager.connect()
        proxy_tools = self._mcp_manager.get_proxy_tools()
        self._apply_mcp_proxy_tools(proxy_tools)
        self._mcp_connected = True
        return True

    def _apply_mcp_proxy_tools(self, proxy_tools: List[BaseTool]) -> None:
        """
        将 MCP 代理工具写入当前 Core 的 ToolRegistry，并同步到 tool_catalog。

        Kernel/CorePool 下 search_tools / call_tool 绑定的是「全量 catalog」副本；
        若不同步，MCP 工具只会出现在 _tool_registry 中，导致搜不到、call_tool 报不存在。
        """
        self._tool_registry.update_tools(cast(List[BaseTool], proxy_tools))
        cat = self._tool_catalog
        if cat is not None and cat is not self._tool_registry:
            cat.update_tools(cast(List[BaseTool], proxy_tools))

    def _build_runtime_mcp_servers(
        self, servers: List[MCPServerConfig]
    ) -> List[MCPServerConfig]:
        """
        构建运行期 MCP servers：
        - 保留用户配置
        - 仅当 mcp.inject_builtin_schedule_mcp 为 True 且未显式配置本地 mcp_server.py 时，自动追加 schedule_tools
        """
        runtime_servers = [s.model_copy(deep=True) for s in servers]

        if not self._config.mcp.inject_builtin_schedule_mcp:
            return runtime_servers

        script_path = Path(__file__).resolve().parents[4] / "mcp_server.py"
        script_path_str = str(script_path)
        project_root = str(script_path.parent)
        project_src = str(script_path.parent / "src")

        has_local_server = any(
            (
                server.name == "schedule_tools"
                or (
                    server.command in {"python", "python3", sys.executable}
                    and script_path_str in server.args
                )
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
