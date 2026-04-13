"""
Subagent 工具集 — 通用异步 multi-agent 通信。

提供 8 个工具：
  1. create_subagent            — 创建单个子 agent 执行任务（fire-and-forget）
  2. create_parallel_subagents  — 并行创建多个子 agent（first-done 语义）
  3. list_agents                — 系统级进程表快照（按 scope 过滤，用于寻址 P2P）
  4. send_message_to_agent      — 向任意已知 session 发送 P2P 消息
  5. reply_to_message           — 回复收到的 query 消息
  6. get_subagent_status        — 查询子 agent 状态（只读，不取消、不收割）
  7. reap_subagent              — 对已结束的子任务 waitpid：取回完整结果并收割 zombie、删工作区
  8. cancel_subagent            — 取消正在运行的子 agent

设计原则：
- 工具本身不阻塞：create_subagent 立即返回 {subagent_id, status:"running"}
- first-done 天然成立：每个 subagent 完成后独立 inject_turn 唤醒父 session
- P2P 寻址：通过 session_id 直接向任意已知 agent 发送消息
- Sub agent 只注册 send_message_to_agent + reply_to_message（mode="sub" 时防递归孵化）

执行上下文（由 Kernel 注入到 __execution_context__）：
  - session_id: 当前 agent 所在 session（用作 sender / parent）
  - source: 前端标识
  - user_id: 用户 ID
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from system.multi_agent.constants import METADATA_KEY_AGENT_MESSAGE, P2P_REQUEST_FRONTEND_TAG

if TYPE_CHECKING:
    from system.kernel.core_pool import CoreEntry, CorePool
    from system.kernel.scheduler import KernelScheduler

logger = logging.getLogger(__name__)


def _guard_subagent_parent(
    core_pool: "CorePool",
    *,
    exec_ctx: Dict[str, Any],
    entry: "CoreEntry",
) -> Optional[ToolResult]:
    """仅创建该 sub 的父会话可执行 get / reap / cancel。"""
    my_sid = str(exec_ctx.get("session_id") or "").strip()
    if not my_sid:
        return ToolResult(
            success=False,
            message="缺少 __execution_context__.session_id，无法校验父权",
            error="MISSING_EXECUTION_CONTEXT",
        )
    parent = (entry.parent_session_id or "").strip()
    if not parent:
        return None
    if parent != my_sid:
        return ToolResult(
            success=False,
            message="无权操作该子 Agent：仅创建该子任务的父会话可执行此操作",
            error="FORBIDDEN_NOT_YOUR_SUB",
        )
    return None


# 子 Agent 必须能调用的通信工具，用于向父 Agent 汇报结果。创建 subagent 时若指定了
# allowed_tools，会自动合并此列表，避免父 Agent 配置遗漏导致子 Agent 无法汇报。
SUBAGENT_COMMUNICATION_TOOLS = ["send_message_to_agent", "reply_to_message"]


def _merge_allowed_tools_for_subagent(
    allowed_tools: Optional[List[str]],
    *,
    add_bash: bool = False,
) -> Optional[List[str]]:
    """
    合并 allowed_tools，确保子 Agent 始终能向父 Agent 汇报。

    若父 Agent 指定了 allowed_tools 但遗漏 send_message_to_agent / reply_to_message，
    子 Agent 将无法发送结果。此函数自动补全，避免配置错误。
    add_bash=True 且配置允许时，会加入 bash（子 Agent 内仍受 BashSecurity 白名单限制）。
    """
    if allowed_tools is None:
        return None
    result = list(allowed_tools)
    for t in SUBAGENT_COMMUNICATION_TOOLS:
        if t not in result:
            result.append(t)
    if add_bash and "bash" not in result:
        result.append("bash")
    return result


def _build_subagent_limit_fail_msg(
    *,
    reason: str,
    subagent_id: str,
    log_dir: str,
    limit_type: str,
) -> str:
    """构建子任务被系统限制终止时的完整提示，含取消原因、日志位置与主 Agent 建议。"""
    log_pattern = f"session-subagent:{subagent_id}-*.jsonl"
    return (
        f"[子任务 {subagent_id} 被系统终止]\n\n"
        f"**取消原因**: {reason}\n\n"
        f"**日志位置**: {log_dir}\n"
        f"**日志文件名匹配**: {log_pattern}\n\n"
        f"**建议主 Agent**: 使用 bash 执行 `tail -n 100 {log_dir}/session-subagent:{subagent_id}-*.jsonl` "
        f"或 `ls -t {log_dir}/session-subagent:{subagent_id}-*.jsonl | head -1 | xargs tail -n 100` "
        f"读取日志尾部，检查子任务进展后决定是否调整 config 中的 {limit_type} 限额并重启子任务。"
    )


# ---------------------------------------------------------------------------
# 共用的后台 subagent 任务函数
# ---------------------------------------------------------------------------


async def _run_subagent_task(
    *,
    subagent_id: str,
    sub_session_id: str,
    task_description: str,
    parent_session_id: str,
    core_pool: "CorePool",
    scheduler: "KernelScheduler",
    allowed_tools: Optional[List[str]] = None,
    context: Optional[str] = None,
    max_iterations: int = 8,
) -> None:
    """
    后台运行 subagent 的完整生命周期。

    1. 通过 KernelScheduler.submit() 驱动 sub_session 处理任务
    2. 完成后调用 core_pool.on_sub_complete() → inject_turn 唤醒父 session
    3. 失败后调用 core_pool.on_sub_fail() → inject_turn 通知父 session
    4. 无论成功失败，最后 evict 清理资源
    """
    from agent_core.config import get_config
    from agent_core.kernel_interface import KernelRequest, CoreProfile

    config = get_config()
    agent_cfg = getattr(config, "agent", None)
    cmd_cfg = getattr(config, "command_tools", None)
    allow_run_for_subagent = bool(
        cmd_cfg and getattr(cmd_cfg, "allow_run_for_subagent", False)
    )
    subagent_max_seconds = getattr(
        agent_cfg, "subagent_max_seconds", 600
    )
    subagent_max_tokens = getattr(
        agent_cfg, "subagent_max_tokens", 500_000
    )
    subagent_max_context_tokens = getattr(
        agent_cfg, "subagent_max_context_tokens", None
    )
    subagent_max_iterations_default = getattr(
        agent_cfg, "subagent_max_iterations", 15
    )
    max_iter_override = max_iterations if max_iterations else subagent_max_iterations_default

    # 构造 subagent 的 CoreProfile（mode="sub"，按 allowed_tools 限制 + 时间/token 上限）
    # 若配置允许，为子 Agent 开放 bash（执行时仍受 BashSecurity 白名单限制）
    effective_allowed = _merge_allowed_tools_for_subagent(
        allowed_tools, add_bash=allow_run_for_subagent
    )
    profile = CoreProfile.default_sub(
        allowed_tools=effective_allowed,
        frontend_id="subagent",
        dialog_window_id=subagent_id,
        max_iterations_override=max_iter_override,
        max_total_tokens=subagent_max_tokens,
        max_context_tokens=subagent_max_context_tokens,
        allow_dangerous_commands=allow_run_for_subagent,
        tools_config=getattr(config, "tools", None),
    )
    # 24h TTL 保护（任务完成后主动 evict，不依赖 TTL 扫描）
    profile.session_expired_seconds = 86400

    # 构建任务文本（若有 context，前置说明）
    task_text = task_description
    if context:
        task_text = f"{context}\n\n---\n\n{task_text}"
    # 在 context 中注入父 session_id 与通信规则
    task_text = (
        f"[系统信息] 你是子 Agent，subagent_id={subagent_id}，父 session_id={parent_session_id}。\n\n"
        f"**完成信号**：任务完成后，将结果作为最终回复返回即可，系统会**自动**向父 Agent 推送完成通知，"
        f"**切勿**用 send_message_to_agent 汇报完成，否则会导致重复通知。\n\n"
        f"**send_message_to_agent** 仅用于：需要向父 Agent **询问**任务细节、实现要求、澄清歧义时。\n\n"
        + task_text
    )

    task_preview = (task_description or "")[:60].replace("\n", " ")
    logger.info(
        "subagent task started subagent_id=%s parent_session_id=%s task_preview=%s",
        subagent_id,
        parent_session_id,
        task_preview + ("..." if len(task_description or "") > 60 else ""),
        extra={"subagent_id": subagent_id, "parent_session_id": parent_session_id},
    )

    request = KernelRequest.create(
        text=task_text,
        session_id=sub_session_id,
        frontend_id="subagent",
        priority=-1,
        metadata={"source": "subagent", "user_id": subagent_id},
        profile=profile,
    )

    try:
        logger.debug(
            "subagent submitting request subagent_id=%s sub_session_id=%s",
            subagent_id,
            sub_session_id,
        )
        submit_handle = await scheduler.submit(request)
        try:
            run_result = await scheduler.wait_result(
                submit_handle, timeout_seconds=float(subagent_max_seconds)
            )
        except asyncio.TimeoutError:
            scheduler.cancel_session_tasks(sub_session_id)
            log_dir = getattr(
                getattr(config, "logging", None), "session_log_dir", "./logs/sessions"
            )
            timeout_msg = _build_subagent_limit_fail_msg(
                reason=f"子任务执行超时（已超过 {subagent_max_seconds} 秒），已强制终止",
                subagent_id=subagent_id,
                log_dir=log_dir,
                limit_type="subagent_max_seconds",
            )
            logger.warning(
                "subagent task timed out subagent_id=%s parent_session_id=%s limit_seconds=%s",
                subagent_id,
                parent_session_id,
                subagent_max_seconds,
                extra={"subagent_id": subagent_id, "parent_session_id": parent_session_id},
            )
            core_pool.on_sub_fail(sub_session_id, timeout_msg)
            return
        result_text = run_result.output_text or ""
        logger.info(
            "subagent task finished successfully subagent_id=%s parent_session_id=%s result_len=%s",
            subagent_id,
            parent_session_id,
            len(result_text),
            extra={"subagent_id": subagent_id, "parent_session_id": parent_session_id},
        )
        core_pool.on_sub_complete(sub_session_id, result_text)
    except asyncio.CancelledError:
        logger.info(
            "subagent task cancelled subagent_id=%s parent_session_id=%s",
            subagent_id,
            parent_session_id,
            extra={"subagent_id": subagent_id, "parent_session_id": parent_session_id},
        )
        # CancelledError 会由 core_pool.cancel_sub() 更新状态，不调用 on_fail
        raise
    except Exception as exc:
        logger.exception(
            "subagent task failed subagent_id=%s parent_session_id=%s error=%s",
            subagent_id,
            parent_session_id,
            exc,
            extra={"subagent_id": subagent_id, "parent_session_id": parent_session_id},
        )
        core_pool.on_sub_fail(sub_session_id, str(exc))
    finally:
        # 清理 subagent session 资源（无论成功/失败/取消）
        try:
            await core_pool.evict(sub_session_id)
        except Exception as exc:
            logger.debug(
                "_run_subagent_task: evict failed for session %s: %s",
                sub_session_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Tool 1: create_subagent
# ---------------------------------------------------------------------------


class CreateSubagentTool(BaseTool):
    """创建单个子 Agent 异步执行任务，立即返回 subagent_id。"""

    def __init__(
        self,
        core_pool: "CorePool",
        scheduler: "KernelScheduler",
    ) -> None:
        self._core_pool = core_pool
        self._scheduler = scheduler

    @property
    def name(self) -> str:
        return "create_subagent"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="create_subagent",
            description=(
                "创建单个子 Agent（subagent）作为后台子会话异步执行任务，立即返回 `subagent_id`。\n\n"
                "调用本工具只会完成“派生子任务”这一步，不会等待子任务产出最终结果；"
                "子任务会在系统进程表中独立运行，父 Agent 可继续当前工作流。\n\n"
                "子任务完成或失败后，系统会向父 Agent 注入一条通知消息，只包含状态与结果预览。"
                "若需要只读完整文本，调用 `get_subagent_status(..., include_full_result=True)`；"
                "确认不再需要保留 zombie 与磁盘产物后，调用 `reap_subagent(subagent_id=...)` 完成收割。"
            ),
            parameters=[
                ToolParameter(
                    name="task",
                    type="string",
                    description="子 Agent 需要执行的任务描述。应明确目标、约束、产出格式，以及完成标准。",
                    required=True,
                ),
                ToolParameter(
                    name="allowed_tools",
                    type="array",
                    description=(
                        "子 Agent 可用的工具名称列表。留空（null）表示使用 sub 模式默认权限。\n\n"
                        "⚠️ 权限配置说明：\n"
                        "- 系统会**自动加入** send_message_to_agent、reply_to_message，确保子 Agent 在需要澄清时能联系父 Agent\n"
                        "- bash 可由配置 command_tools.allow_run_for_subagent 开启，开启后仅可执行白名单内只读命令（禁止管道、重定向与危险命令）\n"
                        "- 常用组合示例：[\"read_file\", \"search_tools\"] 用于代码/文档分析；[\"read_file\", \"bash\"] 需配置开启子 Agent 命令行后使用"
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="context",
                    type="string",
                    description=(
                        "传递给子 Agent 的背景信息或约束（例如：'分析角度为技术可行性'）。"
                        "建议写明父 Agent 之后会如何使用结果，这有助于子 Agent 选择合适的输出粒度。"
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="max_iterations",
                    type="integer",
                    description="子 Agent 最大迭代次数（默认从 config.yaml 的 agent.subagent_max_iterations 读取，配置未设置时默认 50）",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "异步分析一份文档",
                    "params": {
                        "task": "阅读 /data/report.md 并提取关键数据点，以结构化列表输出",
                        "context": "完成后将结果汇总到主 Agent 正在撰写的分析报告中",
                    },
                },
                {
                    "description": "限制工具集的子任务",
                    "params": {
                        "task": "搜索近期关于量子计算的新闻并总结",
                        "allowed_tools": ["search_web"],
                    },
                },
            ],
            usage_notes=[
                "create_subagent 只负责创建后台子任务，立即返回，不等待执行结束",
                "子 Agent 完成后，父 Agent 只会收到预览通知，不会自动拿到完整输出",
                "如果预览不足以继续决策，可调用 get_subagent_status(include_full_result=True) 只读拉取完整文本",
                "对已结束子任务需释放资源时调用 reap_subagent；查询状态用 get_subagent_status；终止运行中用 cancel_subagent",
                "sub 模式的子 Agent 不能再创建子 Agent（防止无限递归）",
                "若需并行多个子任务，使用 create_parallel_subagents 更高效",
            ],
            tags=["multi-agent", "subagent", "async"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        from agent_core.config import get_config

        task = kwargs.get("task", "").strip()
        if not task:
            return ToolResult(
                success=False,
                message="缺少 task 参数",
                error="MISSING_TASK",
            )

        allowed_tools: Optional[List[str]] = kwargs.get("allowed_tools")
        context: Optional[str] = kwargs.get("context")
        
        # max_iterations：优先使用传入值，否则从配置读取，最后兜底 50
        max_iterations_param = kwargs.get("max_iterations")
        if max_iterations_param is not None:
            max_iterations = int(max_iterations_param)
        else:
            config = get_config()
            max_iterations = getattr(
                getattr(config, "agent", None),
                "subagent_max_iterations",
                50  # 兜底值
            )

        # 从 __execution_context__ 读取父 session_id
        exec_ctx: Dict[str, Any] = kwargs.get("__execution_context__") or {}
        parent_session_id: str = exec_ctx.get("session_id", "")

        if not parent_session_id:
            logger.error(
                "create_subagent: parent_session_id is empty — __execution_context__ not injected. "
                "This happens when called via call_tool without __execution_context__ forwarding."
            )
            return ToolResult(
                success=False,
                message="无法创建子 Agent：父 session_id 为空（__execution_context__ 未正确传递）",
                error="MISSING_PARENT_SESSION",
            )

        subagent_id = str(uuid.uuid4())[:12]
        sub_session_id = f"sub:{subagent_id}"

        info = self._core_pool.register_sub(
            sub_session_id=sub_session_id,
            parent_session_id=parent_session_id,
            task_description=task,
        )

        bg = asyncio.create_task(
            _run_subagent_task(
                subagent_id=subagent_id,
                sub_session_id=sub_session_id,
                task_description=task,
                parent_session_id=parent_session_id,
                core_pool=self._core_pool,
                scheduler=self._scheduler,
                allowed_tools=allowed_tools,
                context=context,
                max_iterations=max_iterations,
            ),
            name=f"subagent-{subagent_id}",
        )
        info.bg_task = bg

        task_preview = (task or "")[:50].replace("\n", " ")
        logger.info(
            "create_subagent: spawned subagent_id=%s parent_session_id=%s task_preview=%s",
            subagent_id,
            parent_session_id,
            task_preview + ("..." if len(task) > 50 else ""),
            extra={"subagent_id": subagent_id, "parent_session_id": parent_session_id},
        )

        return ToolResult(
            success=True,
            data={"subagent_id": subagent_id, "status": "running"},
            message=f"子 Agent 已创建，subagent_id={subagent_id}，正在后台执行任务。",
        )


# ---------------------------------------------------------------------------
# Tool 2: create_parallel_subagents
# ---------------------------------------------------------------------------


class CreateParallelSubagentsTool(BaseTool):
    """并行创建多个子 Agent，各自独立完成任务（first-done 语义）。"""

    def __init__(
        self,
        core_pool: "CorePool",
        scheduler: "KernelScheduler",
    ) -> None:
        self._core_pool = core_pool
        self._scheduler = scheduler

    @property
    def name(self) -> str:
        return "create_parallel_subagents"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="create_parallel_subagents",
            description=(
                "并行创建多个子 Agent，分别作为独立后台子会话运行。\n\n"
                "每个子任务都会独立完成、独立通知父 Agent，不需要等待全部结束后再继续；"
                "父 Agent 可以在收到第一个满意结果后取消其余任务，也可以继续汇总多个结果。\n\n"
                "适合多角度分析、方案比较、并行搜索、并行草拟等场景。"
            ),
            parameters=[
                ToolParameter(
                    name="tasks",
                    type="array",
                    description=(
                        "任务列表，每项为对象，包含：\n"
                        "  - task (string, 必填): 任务描述\n"
                        "  - allowed_tools (array, 可选): 工具列表；系统会自动加入 send_message_to_agent、reply_to_message\n"
                        "  - context (string, 可选): 背景信息\n"
                        "  - max_iterations (integer, 可选): 最大迭代次数（默认从 config.yaml 的 agent.subagent_max_iterations 读取，配置未设置时默认 50）"
                    ),
                    required=True,
                ),
            ],
            examples=[
                {
                    "description": "从三个角度并行分析同一主题",
                    "params": {
                        "tasks": [
                            {"task": "从技术可行性角度分析量子计算的商业化前景"},
                            {"task": "从市场规模角度分析量子计算的商业化前景"},
                            {"task": "从竞争格局角度分析量子计算的商业化前景"},
                        ]
                    },
                },
            ],
            usage_notes=[
                "各子 Agent 相互独立，谁先完成谁先通知父 Agent",
                "通知只带预览；只读完整内容用 get_subagent_status(include_full_result=True)；收割 zombie 用 reap_subagent",
                "若已有足够好的结果，应主动 cancel_subagent 取消其余任务，减少资源消耗",
                "任务数量建议不超过 5 个，避免资源过度消耗",
            ],
            tags=["multi-agent", "parallel", "async"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        from agent_core.config import get_config
        import json as _json
        tasks_raw = kwargs.get("tasks")
        # LLM 有时会把 tasks 序列化成 JSON 字符串，尝试解析
        if isinstance(tasks_raw, str):
            try:
                tasks_raw = _json.loads(tasks_raw)
            except Exception:
                pass
        if not tasks_raw or not isinstance(tasks_raw, list):
            return ToolResult(
                success=False,
                message="缺少 tasks 参数或格式不正确（需为数组）",
                error="MISSING_TASKS",
            )

        exec_ctx: Dict[str, Any] = kwargs.get("__execution_context__") or {}
        parent_session_id: str = exec_ctx.get("session_id", "")

        if not parent_session_id:
            logger.error(
                "create_parallel_subagents: parent_session_id is empty — __execution_context__ not injected. "
                "This happens when called via call_tool without __execution_context__ forwarding."
            )
            return ToolResult(
                success=False,
                message="无法创建子 Agent：父 session_id 为空（__execution_context__ 未正确传递）",
                error="MISSING_PARENT_SESSION",
            )

        # 从配置读取默认 max_iterations
        config = get_config()
        default_max_iterations = getattr(
            getattr(config, "agent", None),
            "subagent_max_iterations",
            50  # 兜底值
        )

        subagent_ids = []
        for item in tasks_raw:
            if not isinstance(item, dict):
                continue
            task_desc = (item.get("task") or "").strip()
            if not task_desc:
                continue

            subagent_id = str(uuid.uuid4())[:12]
            sub_session_id = f"sub:{subagent_id}"
            allowed_tools = item.get("allowed_tools")
            context = item.get("context")
            
            # max_iterations：优先使用传入值，否则从配置读取
            max_iterations_param = item.get("max_iterations")
            if max_iterations_param is not None:
                max_iterations = int(max_iterations_param)
            else:
                max_iterations = default_max_iterations

            info = self._core_pool.register_sub(
                sub_session_id=sub_session_id,
                parent_session_id=parent_session_id,
                task_description=task_desc,
            )

            bg = asyncio.create_task(
                _run_subagent_task(
                    subagent_id=subagent_id,
                    sub_session_id=sub_session_id,
                    task_description=task_desc,
                    parent_session_id=parent_session_id,
                    core_pool=self._core_pool,
                    scheduler=self._scheduler,
                    allowed_tools=allowed_tools,
                    context=context,
                    max_iterations=max_iterations,
                ),
                name=f"subagent-{subagent_id}",
            )
            info.bg_task = bg
            subagent_ids.append(subagent_id)

        if not subagent_ids:
            return ToolResult(
                success=False,
                message="tasks 列表中没有有效任务",
                error="NO_VALID_TASKS",
            )

        logger.info(
            "create_parallel_subagents: spawned count=%s parent_session_id=%s subagent_ids=%s",
            len(subagent_ids),
            parent_session_id,
            subagent_ids,
            extra={"parent_session_id": parent_session_id, "subagent_ids": subagent_ids, "count": len(subagent_ids)},
        )

        return ToolResult(
            success=True,
            data={"subagent_ids": subagent_ids, "count": len(subagent_ids), "status": "running"},
            message=(
                f"已并行创建 {len(subagent_ids)} 个子 Agent："
                f"{subagent_ids}。各自完成后将依次通知。"
            ),
        )


# ---------------------------------------------------------------------------
# Tool 3: send_message_to_agent
# ---------------------------------------------------------------------------


class SendMessageToAgentTool(BaseTool):
    """向任意已知 session 发送 P2P 消息；默认阻塞至对方 reply_to_message（require_reply=False 为仅投递）。"""

    def __init__(self, scheduler: "KernelScheduler") -> None:
        self._scheduler = scheduler

    @property
    def name(self) -> str:
        return "send_message_to_agent"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="send_message_to_agent",
            description=(
                "向任意已知 session 发送 P2P 消息。\n\n"
                "消息立即投递（inject_turn），目标 session 会被唤醒处理。\n"
                "- **require_reply=True（默认）**：阻塞当前工具调用，直到对方使用 `reply_to_message` 回复同一 message_id；"
                "工具结果中包含 `reply_content`。超时见配置 `agent.p2p_reply_timeout_seconds`。\n"
                "- **require_reply=False**：不等待对方，仅返回 message_id（单向通知）。\n\n"
                "**子 Agent 注意**：完成结果由系统自动推送，本工具**仅用于向父询问**任务细节、实现要求、澄清歧义，"
                "**切勿**用于汇报完成，否则会导致重复通知。\n\n"
                "主 Agent 可用于向子任务补充信息、对子任务发指令、或与其他已知 session 协调。"
            ),
            parameters=[
                ToolParameter(
                    name="session_id",
                    type="string",
                    description=(
                        "目标 Agent 的 session_id（如 'cli:root', 'shuiyuan:Osc7', 'sub:abc123'）"
                    ),
                    required=True,
                ),
                ToolParameter(
                    name="content",
                    type="string",
                    description="消息内容（自然语言）",
                    required=True,
                ),
                ToolParameter(
                    name="require_reply",
                    type="boolean",
                    description=(
                        "是否需要对方回复（默认 True，阻塞直到 reply_to_message 或超时）。"
                        "设为 False 时仅单向通知，立即返回 message_id。"
                    ),
                    required=False,
                    default=True,
                ),
                ToolParameter(
                    name="reply_timeout_seconds",
                    type="number",
                    description=(
                        "仅当 require_reply=True 时有效：最长等待秒数；"
                        "不传则使用配置 agent.p2p_reply_timeout_seconds（默认 600）。"
                    ),
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "子 Agent 向父询问任务细节（默认等待回复，不用于汇报完成）",
                    "params": {
                        "session_id": "cli:root",
                        "content": "任务描述中「大厂」具体指哪些公司？是否需要包含外企？",
                    },
                },
                {
                    "description": "仅通知、不要求对方回复（fire-and-forget）",
                    "params": {
                        "session_id": "shuiyuan:Osc7",
                        "content": "子任务进度：已拉取日志，正在分析。",
                        "require_reply": False,
                    },
                },
            ],
            usage_notes=[
                "默认 require_reply=True：阻塞至 reply_to_message 或超时；单向通知请显式设 require_reply=False",
                "对方须用 reply_to_message(correlation_id=你的 message_id) 完成同步回复",
                "目标 session 必须已存在（在 CorePool 中或可从 checkpoint 恢复）",
                "子 Agent 可从 __execution_context__.session_id 获取自身 session_id",
                "子 Agent：完成信号由系统自动推送；本工具只用于询问、澄清、补充协作上下文",
            ],
            tags=["multi-agent", "p2p", "messaging"],
        )

    def _check_sender_cancelled(self, sender_session_id: str) -> Optional[ToolResult]:
        """若发送者是已取消的 subagent 则返回拒绝结果；否则返回 None。

        子类（如 _LazySchedulerSendMessageTool）可 override 以接入 CorePool 子任务状态。
        """
        return None

    async def execute(self, **kwargs: Any) -> ToolResult:
        target_session_id = (kwargs.get("session_id") or "").strip()
        content = (kwargs.get("content") or "").strip()
        _rr = kwargs.get("require_reply")
        require_reply = True if _rr is None else bool(_rr)
        reply_timeout_raw = kwargs.get("reply_timeout_seconds")
        reply_timeout_seconds: Optional[float] = None
        if reply_timeout_raw is not None:
            try:
                reply_timeout_seconds = float(reply_timeout_raw)
            except (TypeError, ValueError):
                reply_timeout_seconds = None

        if not target_session_id:
            return ToolResult(
                success=False, message="缺少 session_id 参数", error="MISSING_SESSION_ID"
            )
        if not content:
            return ToolResult(
                success=False, message="缺少 content 参数", error="MISSING_CONTENT"
            )

        exec_ctx: Dict[str, Any] = kwargs.get("__execution_context__") or {}
        sender_session_id: str = exec_ctx.get("session_id", "unknown")

        # 纵深防御：子类可 override _check_sender_cancelled 拒绝已取消的 subagent 发消息
        reject = self._check_sender_cancelled(sender_session_id)
        if reject is not None:
            return reject

        from agent_core.kernel_interface.action import AgentMessage, KernelRequest

        message_id = str(uuid.uuid4())
        msg_type = "query" if require_reply else "notify"
        agent_msg = AgentMessage(
            message_id=message_id,
            sender_session=sender_session_id,
            receiver_session=target_session_id,
            message_type=msg_type,
            require_reply=require_reply,
        )

        # 构建发往目标 session 的文本
        sender_label = f"来自 [{sender_session_id}] 的消息"
        if require_reply:
            inject_text = (
                f"[{sender_label}（message_id={message_id}，需要回复）]\n\n{content}"
            )
        else:
            inject_text = f"[{sender_label}]\n\n{content}"

        request = KernelRequest.create(
            text=inject_text,
            session_id=target_session_id,
            frontend_id=P2P_REQUEST_FRONTEND_TAG,
            priority=-1,
            metadata={METADATA_KEY_AGENT_MESSAGE: agent_msg},
        )

        reply_waiter: Optional[asyncio.Future[str]] = None
        if require_reply:
            reply_waiter = self._scheduler.register_p2p_reply_waiter(message_id)

        try:
            self._scheduler.inject_turn(request)
        except Exception as exc:
            if reply_waiter is not None:
                self._scheduler.cancel_p2p_reply_waiter(message_id)
            logger.exception(
                "send_message_to_agent: inject_turn failed target=%s: %s",
                target_session_id,
                exc,
            )
            return ToolResult(
                success=False,
                message=f"消息投递失败：{exc}",
                error="INJECT_FAILED",
            )

        logger.info(
            "send_message_to_agent: %s → %s message_id=%s require_reply=%s",
            sender_session_id,
            target_session_id,
            message_id,
            require_reply,
        )

        if not require_reply or reply_waiter is None:
            return ToolResult(
                success=True,
                data={"message_id": message_id, "target_session": target_session_id},
                message=f"消息已发送至 {target_session_id}（message_id={message_id}）",
            )

        from agent_core.config import get_config

        cfg = get_config()
        default_to = float(getattr(cfg.agent, "p2p_reply_timeout_seconds", 600))
        wait_to = (
            reply_timeout_seconds
            if reply_timeout_seconds is not None and reply_timeout_seconds > 0
            else default_to
        )

        try:
            reply_text = await asyncio.wait_for(reply_waiter, timeout=wait_to)
        except asyncio.TimeoutError:
            self._scheduler.cancel_p2p_reply_waiter(message_id)
            return ToolResult(
                success=False,
                data={"message_id": message_id, "target_session": target_session_id},
                message=(
                    f"等待 {target_session_id} 回复超时（{wait_to:.0f}s），"
                    f"message_id={message_id}"
                ),
                error="P2P_REPLY_TIMEOUT",
            )
        except asyncio.CancelledError:
            self._scheduler.cancel_p2p_reply_waiter(message_id)
            raise

        return ToolResult(
            success=True,
            data={
                "message_id": message_id,
                "target_session": target_session_id,
                "reply_content": reply_text,
            },
            message=(
                f"已收到来自 {target_session_id} 的回复（message_id={message_id}）"
            ),
        )


# ---------------------------------------------------------------------------
# Tool 4: reply_to_message
# ---------------------------------------------------------------------------


class ReplyToMessageTool(BaseTool):
    """回复收到的 query 消息，通过 correlation_id 关联原消息。"""

    def __init__(self, scheduler: "KernelScheduler") -> None:
        self._scheduler = scheduler

    @property
    def name(self) -> str:
        return "reply_to_message"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="reply_to_message",
            description=(
                "回复收到的消息，将 content 发回原发送方。\n\n"
                "需要提供收到消息的 correlation_id（即原消息的 message_id）\n"
                "以及原发送方的 sender_session_id，这两个值在接收消息时系统会提供。\n\n"
                "当收到带 require_reply=True 的 query 消息时，用此工具沿原对话链路回复。"
            ),
            parameters=[
                ToolParameter(
                    name="correlation_id",
                    type="string",
                    description="原消息的 message_id（从收到的消息信息中获取）",
                    required=True,
                ),
                ToolParameter(
                    name="sender_session_id",
                    type="string",
                    description="原消息发送方的 session_id（从收到的消息信息中获取）",
                    required=True,
                ),
                ToolParameter(
                    name="content",
                    type="string",
                    description="回复内容",
                    required=True,
                ),
            ],
            examples=[
                {
                    "description": "回复一条查询请求",
                    "params": {
                        "correlation_id": "abc123-uuid",
                        "sender_session_id": "cli:root",
                        "content": "水源今日最热门技术帖：1. XXX 2. YYY 3. ZZZ",
                    },
                },
            ],
            usage_notes=[
                "correlation_id 即收到消息时的 message_id，可从消息元数据中读取",
                "sender_session_id 在收到消息时由系统注入（[来自 {sender} 的消息]）",
                "若对方 send_message 时 require_reply=True：只唤醒阻塞中的工具并返回正文，不再向对方会话 inject 重复一轮",
                "若只是子任务正常完成，不要用 reply_to_message 汇报；完成通知会由系统自动处理",
            ],
            tags=["multi-agent", "p2p", "messaging"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        correlation_id = (kwargs.get("correlation_id") or "").strip()
        sender_session_id = (kwargs.get("sender_session_id") or "").strip()
        content = (kwargs.get("content") or "").strip()

        if not correlation_id:
            return ToolResult(
                success=False, message="缺少 correlation_id 参数", error="MISSING_CORRELATION_ID"
            )
        if not sender_session_id:
            return ToolResult(
                success=False,
                message="缺少 sender_session_id 参数",
                error="MISSING_SENDER_SESSION",
            )
        if not content:
            return ToolResult(
                success=False, message="缺少 content 参数", error="MISSING_CONTENT"
            )

        exec_ctx: Dict[str, Any] = kwargs.get("__execution_context__") or {}
        my_session_id: str = exec_ctx.get("session_id", "unknown")

        from agent_core.kernel_interface.action import AgentMessage, KernelRequest

        reply_message_id = str(uuid.uuid4())
        agent_msg = AgentMessage(
            message_id=reply_message_id,
            sender_session=my_session_id,
            receiver_session=sender_session_id,
            message_type="reply",
            correlation_id=correlation_id,
        )

        inject_text = (
            f"[来自 [{my_session_id}] 的回复（correlation_id={correlation_id}）]\n\n{content}"
        )

        request = KernelRequest.create(
            text=inject_text,
            session_id=sender_session_id,
            frontend_id=P2P_REQUEST_FRONTEND_TAG,
            priority=-1,
            metadata={METADATA_KEY_AGENT_MESSAGE: agent_msg},
        )

        # send_message(require_reply=True) 已通过工具返回值送达正文；若再 inject_turn，会在首轮 turn
        # 结束并 evict 后排队第二轮请求，导致同一 sub 会话再 load Core、多打一轮 LLM（且日志落到
        # 误用 P2P 标签作 source 的历史问题已修）。存在阻塞等待者时只 complete，不再注入重复 user 轮次。
        if self._scheduler.has_p2p_reply_waiter(correlation_id):
            self._scheduler.complete_p2p_reply(correlation_id, content)
            logger.info(
                "reply_to_message: p2p waiter only (skip inject) %s → %s correlation_id=%s",
                my_session_id,
                sender_session_id,
                correlation_id,
            )
            return ToolResult(
                success=True,
                data={"message_id": reply_message_id, "correlation_id": correlation_id},
                message=f"已唤醒对方阻塞中的 send_message（correlation_id={correlation_id}）",
            )

        try:
            self._scheduler.inject_turn(request)
        except Exception as exc:
            logger.exception(
                "reply_to_message: inject_turn failed target=%s: %s",
                sender_session_id,
                exc,
            )
            return ToolResult(
                success=False,
                message=f"回复投递失败：{exc}",
                error="INJECT_FAILED",
            )

        self._scheduler.complete_p2p_reply(correlation_id, content)

        logger.info(
            "reply_to_message: %s → %s correlation_id=%s",
            my_session_id,
            sender_session_id,
            correlation_id,
        )

        return ToolResult(
            success=True,
            data={"message_id": reply_message_id, "correlation_id": correlation_id},
            message=f"回复已发送至 {sender_session_id}",
        )


# ---------------------------------------------------------------------------
# Tool 5: list_agents（系统级进程表）
# ---------------------------------------------------------------------------


class ListAgentsTool(BaseTool):
    """列出当前进程内 Agent 会话快照（类 OS 进程表），用于寻址 P2P 与管理子任务。"""

    def __init__(self, core_pool: "CorePool") -> None:
        self._core_pool = core_pool

    @property
    def name(self) -> str:
        return "list_agents"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_agents",
            description=(
                "列出本 Kernel 进程内活跃的 Agent 会话快照（内存态，重启后清空）。\n\n"
                "每行含 session_id、parent_session_id、status、profile_mode 等，用于：\n"
                "- **scope=my_children**：仅自己创建的子会话（sub）\n"
                "- **scope=namespace**：与当前会话同一命名空间（沿父链归约到根会话的 memory 键）\n"
                "- **scope=siblings**：与自己同父的其它子会话\n\n"
                "找到目标 session_id 后，用 send_message_to_agent 发起 P2P。"
            ),
            parameters=[
                ToolParameter(
                    name="scope",
                    type="string",
                    description=(
                        "my_children | namespace | siblings；默认 namespace。"
                        "子 Agent（mode=sub）默认不允许 namespace，除非配置开启。"
                    ),
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "查看同命名空间下可协作的会话",
                    "params": {"scope": "namespace"},
                },
                {
                    "description": "仅列出我创建的子任务",
                    "params": {"scope": "my_children"},
                },
            ],
            usage_notes=[
                "仅本进程内会话；不含其它机器或已 reap 的 sub",
                "子 Agent 若需 namespace，需在配置中开启 list_agents_allow_namespace_for_subagent",
            ],
            tags=["multi-agent", "subagent", "registry"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        from agent_core.config import get_config

        from system.multi_agent.registry import (
            build_full_process_table,
            filter_agent_rows,
        )

        raw = (kwargs.get("scope") or "namespace").strip().lower()
        if raw not in ("my_children", "namespace", "siblings"):
            scope = "namespace"
        else:
            scope = raw  # type: ignore[assignment]

        exec_ctx: Dict[str, Any] = kwargs.get("__execution_context__") or {}
        caller_sid = str(exec_ctx.get("session_id") or "").strip()
        if not caller_sid:
            return ToolResult(
                success=False,
                message="缺少当前会话 session_id（__execution_context__ 未注入）",
                error="MISSING_SESSION",
            )

        caller_entry = self._core_pool.get_entry(caller_sid)
        if caller_entry is None:
            return ToolResult(
                success=False,
                message="当前会话不在进程表中（无法列出）",
                error="CALLER_NOT_IN_POOL",
            )

        cfg = get_config()
        prof = caller_entry.profile
        mode = getattr(prof, "mode", None) or "full"
        if mode == "sub" and scope == "namespace":
            if not getattr(cfg.agent, "list_agents_allow_namespace_for_subagent", False):
                return ToolResult(
                    success=False,
                    message=(
                        "子 Agent 默认不允许 scope=namespace；请使用 my_children 或 siblings，"
                        "或在配置 agent.list_agents_allow_namespace_for_subagent 中开启"
                    ),
                    error="NAMESPACE_SCOPE_FORBIDDEN_FOR_SUB",
                )

        full = build_full_process_table(self._core_pool)
        filtered = filter_agent_rows(
            full,
            scope=scope,
            caller_session_id=caller_sid,
            caller_entry=caller_entry,
            pool=self._core_pool,
        )

        return ToolResult(
            success=True,
            data={
                "scope": scope,
                "caller_session_id": caller_sid,
                "agents": filtered,
                "count": len(filtered),
            },
            message=f"共 {len(filtered)} 条会话（scope={scope}）",
        )


# ---------------------------------------------------------------------------
# Tool 6: get_subagent_status
# ---------------------------------------------------------------------------


class GetSubagentStatusTool(BaseTool):
    """查询子 Agent 状态（只读，不会取消、收割或删除工作区）。"""

    def __init__(self, core_pool: "CorePool") -> None:
        self._core_pool = core_pool

    @property
    def name(self) -> str:
        return "get_subagent_status"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_subagent_status",
            description=(
                "查询子 Agent（subagent）的当前状态（**只读**，无任何副作用）。\n\n"
                "默认返回状态摘要和结果预览（前 500 字符）。"
                "设置 include_full_result=true 时，可返回完整输出文本，**仍不会**回收 zombie 或删除磁盘工作区。\n\n"
                "对已结束子任务需要 **waitpid/reap**（从 zombie 表移除并删除 data/workspace/subagent/<id>/ 等）时，"
                "请使用 **reap_subagent**。若需要终止仍在运行的子任务，使用 cancel_subagent。"
            ),
            parameters=[
                ToolParameter(
                    name="subagent_id",
                    type="string",
                    description="要查询的子 Agent 的 subagent_id（由 create_subagent 返回）",
                    required=True,
                ),
                ToolParameter(
                    name="include_full_result",
                    type="boolean",
                    description=(
                        "是否返回子 Agent 的完整输出结果（仅只读，不收割）。\n"
                        "false（默认）：仅返回结果预览（前 500 字符）；\n"
                        "true：在 data 中返回完整 result 文本（若已有）。"
                    ),
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "检查子 Agent 是否已完成（只看状态）",
                    "params": {"subagent_id": "5c1d8838-453"},
                },
                {
                    "description": "只读拉取完整输出（不收割）",
                    "params": {"subagent_id": "5c1d8838-453", "include_full_result": True},
                },
            ],
            usage_notes=[
                "本工具不调用 reap；收割请用 reap_subagent",
                "只能查询本 Agent 创建的子 Agent",
                "status 可能为：running、completed、failed、cancelled",
                "收到 [子任务 xxx 完成] 后，可先 get 预览或只读全文，再决定是否 reap_subagent",
            ],
            tags=["multi-agent", "subagent", "query"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        subagent_id = (kwargs.get("subagent_id") or "").strip()
        if not subagent_id:
            return ToolResult(
                success=False, message="缺少 subagent_id 参数", error="MISSING_SUBAGENT_ID"
            )

        include_full_result = bool(kwargs.get("include_full_result", False))

        sub_session_id = f"sub:{subagent_id}"
        entry = self._core_pool.get_sub_info(sub_session_id)
        if entry is None:
            return ToolResult(
                success=False,
                message=f"未找到 subagent_id={subagent_id}",
                error="SUBAGENT_NOT_FOUND",
            )

        exec_ctx = kwargs.get("__execution_context__") or {}
        denied = _guard_subagent_parent(
            self._core_pool, exec_ctx=exec_ctx, entry=entry
        )
        if denied is not None:
            return denied

        data: Dict[str, Any] = {
            "subagent_id": subagent_id,
            "status": entry.sub_status or "running",
            "parent_session_id": entry.parent_session_id,
            "task_description": (entry.task_description or "")[:100],
            "created_at": entry.created_at,
        }
        if entry.sub_completed_at is not None:
            data["completed_at"] = entry.sub_completed_at
        if entry.sub_result is not None:
            if include_full_result:
                data["result"] = entry.sub_result
            else:
                data["result_preview"] = (entry.sub_result or "")[:500] + (
                    "..." if len(entry.sub_result or "") > 500 else ""
                )
        if entry.sub_error is not None:
            data["error"] = entry.sub_error

        msg_suffix = "（完整结果，只读）" if include_full_result and entry.sub_result is not None else ""
        return ToolResult(
            success=True,
            data=data,
            message=f"子 Agent {subagent_id} 状态：{entry.sub_status or 'running'}{msg_suffix}",
        )


# ---------------------------------------------------------------------------
# Tool 7: reap_subagent
# ---------------------------------------------------------------------------


class ReapSubagentTool(BaseTool):
    """对已结束的子 Agent 执行 waitpid：返回完整结果、回收 zombie、删除子工作区目录。"""

    def __init__(self, core_pool: "CorePool") -> None:
        self._core_pool = core_pool

    @property
    def name(self) -> str:
        return "reap_subagent"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="reap_subagent",
            description=(
                "对已 **结束** 的子 Agent（subagent）执行 **收割（reap）**：\n\n"
                "- 返回完整 `result` 或 `error`（与 get_subagent_status 全文一致）\n"
                "- 从 zombie 表移除 PCB\n"
                "- 删除该 subagent 的隔离工作区目录（data/workspace/subagent/<id>/ 与 /tmp/macchiato/subagent/<id>/）\n\n"
                "仅在子任务状态为 completed / failed / cancelled 时可调用；**仍在运行中**请用 cancel_subagent 或等待完成通知。\n"
                "若只需**只读**查看全文而不回收，请用 get_subagent_status(include_full_result=true)。"
            ),
            parameters=[
                ToolParameter(
                    name="subagent_id",
                    type="string",
                    description="要收割的子 Agent 的 subagent_id（由 create_subagent / create_parallel_subagents 返回）",
                    required=True,
                ),
            ],
            examples=[
                {
                    "description": "子任务已完成，父已不需要再访问其工作区文件，执行收割",
                    "params": {"subagent_id": "5c1d8838-453"},
                },
            ],
            usage_notes=[
                "收割后无法再 get_subagent_status（SUBAGENT_NOT_FOUND）",
                "若仍需要子工作区内的文件，请先 read_file 拷贝到父工作区再 reap",
                "与 get_subagent_status 分工：get 只读；reap 带副作用",
                "若 entry 已消失：仅当**本进程内曾成功 reap 过**同一 id 时才返回成功（幂等）；"
                "否则视为拼写错误或从未创建（SUBAGENT_NOT_FOUND）。进程重启后不保留收割记录。",
            ],
            tags=["multi-agent", "subagent", "lifecycle"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        subagent_id = (kwargs.get("subagent_id") or "").strip()
        if not subagent_id:
            return ToolResult(
                success=False, message="缺少 subagent_id 参数", error="MISSING_SUBAGENT_ID"
            )

        sub_session_id = f"sub:{subagent_id}"
        entry = self._core_pool.get_sub_info(sub_session_id)
        if entry is None:
            if self._core_pool.was_subagent_reaped_in_process(sub_session_id):
                logger.info(
                    "reap_subagent: idempotent subagent_id=%s (reaped earlier in this process)",
                    subagent_id,
                    extra={"subagent_id": subagent_id},
                )
                return ToolResult(
                    success=True,
                    data={"subagent_id": subagent_id, "already_reaped": True},
                    message=(
                        f"subagent_id={subagent_id} 在本进程内已成功收割过，无需重复 reap。"
                    ),
                )
            return ToolResult(
                success=False,
                message=(
                    f"未找到 subagent_id={subagent_id}：请核对是否与 create_subagent 返回的 id 一致；"
                    f"若从未创建过该子任务也会如此。"
                    f"（若曾在**重启前**成功 reap，本进程无记录，属正常。）"
                ),
                error="SUBAGENT_NOT_FOUND",
            )

        exec_ctx = kwargs.get("__execution_context__") or {}
        denied = _guard_subagent_parent(
            self._core_pool, exec_ctx=exec_ctx, entry=entry
        )
        if denied is not None:
            return denied

        status = entry.sub_status or "running"
        if status == "running":
            return ToolResult(
                success=False,
                message=f"子 Agent {subagent_id} 仍在运行，无法收割。请等待完成通知或使用 cancel_subagent。",
                error="SUBAGENT_STILL_RUNNING",
            )

        if status not in {"completed", "failed", "cancelled"}:
            return ToolResult(
                success=False,
                message=f"子 Agent {subagent_id} 状态异常（{status}），无法收割",
                error="SUBAGENT_NOT_REAPABLE",
            )

        data: Dict[str, Any] = {
            "subagent_id": subagent_id,
            "status": status,
            "parent_session_id": entry.parent_session_id,
            "task_description": (entry.task_description or "")[:100],
            "created_at": entry.created_at,
        }
        if entry.sub_completed_at is not None:
            data["completed_at"] = entry.sub_completed_at
        if entry.sub_result is not None:
            data["result"] = entry.sub_result
        if entry.sub_error is not None:
            data["error"] = entry.sub_error

        self._core_pool.reap_zombie(sub_session_id)

        return ToolResult(
            success=True,
            data=data,
            message=f"已收割子 Agent {subagent_id}（状态：{status}）",
        )


# ---------------------------------------------------------------------------
# Tool 8: cancel_subagent
# ---------------------------------------------------------------------------


class CancelSubagentTool(BaseTool):
    """取消正在运行的子 Agent。"""

    def __init__(self, core_pool: "CorePool") -> None:
        self._core_pool = core_pool

    @property
    def name(self) -> str:
        return "cancel_subagent"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="cancel_subagent",
            description=(
                "**取消**正在运行的子 Agent（subagent），会终止其任务。\n\n"
                "在并行子任务中，收到第一个满意结果后可取消其余子 Agent 节省资源。\n"
                "已完成、失败或已取消的子 Agent 调用此工具不会报错。\n\n"
                "**不删除磁盘**：本工具**不会**删除子工作区目录；与 `completed`/`failed` 一样，删盘仅在调用 "
                "`reap_subagent` 时发生。\n\n"
                "⚠️ 仅查询状态或只读全文请用 get_subagent_status；收割 zombie 与删除子工作区请用 reap_subagent。"
            ),
            parameters=[
                ToolParameter(
                    name="subagent_id",
                    type="string",
                    description="要取消的子 Agent 的 subagent_id（由 create_subagent 返回）",
                    required=True,
                ),
            ],
            examples=[
                {
                    "description": "在收到第一个结果后取消其余并行子任务",
                    "params": {"subagent_id": "abc123456789"},
                },
            ],
            usage_notes=[
                "只能取消本 Agent 创建的子 Agent",
                "取消操作是尽力而为（best-effort），任务可能已完成",
                "取消后不会再收到该子 Agent 的完成通知",
                "若任务其实已经结束，cancel_subagent 只会返回当前最终状态，不会报错",
                "取消不会删 `data/workspace/subagent/<id>/` 等目录；不再需要时对该 id 调用 reap_subagent",
            ],
            tags=["multi-agent", "subagent", "cancel"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        subagent_id = (kwargs.get("subagent_id") or "").strip()
        if not subagent_id:
            return ToolResult(
                success=False, message="缺少 subagent_id 参数", error="MISSING_SUBAGENT_ID"
            )

        sub_session_id = f"sub:{subagent_id}"
        info = self._core_pool.get_sub_info(sub_session_id)
        if info is None:
            return ToolResult(
                success=False,
                message=f"未找到 subagent_id={subagent_id}",
                error="SUBAGENT_NOT_FOUND",
            )

        exec_ctx = kwargs.get("__execution_context__") or {}
        denied = _guard_subagent_parent(self._core_pool, exec_ctx=exec_ctx, entry=info)
        if denied is not None:
            return denied

        cancelled = self._core_pool.cancel_sub(sub_session_id)
        final_status = self._core_pool.get_sub_info(sub_session_id)
        status_str = final_status.sub_status if final_status and final_status.sub_status else "unknown"
        parent_session_id = info.parent_session_id if info else ""

        logger.info(
            "cancel_subagent: subagent_id=%s parent_session_id=%s cancelled=%s final_status=%s",
            subagent_id,
            parent_session_id,
            cancelled,
            status_str,
            extra={"subagent_id": subagent_id, "parent_session_id": parent_session_id, "status": status_str},
        )

        return ToolResult(
            success=True,
            data={"subagent_id": subagent_id, "status": status_str},
            message=(
                f"子 Agent {subagent_id} 已{'取消' if cancelled else '处理'}（当前状态：{status_str}）"
            ),
        )
