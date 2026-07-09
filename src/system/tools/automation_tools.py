"""Automation control and query tools."""

from __future__ import annotations

from datetime import datetime
import json
import time
from typing import Any, Optional
from zoneinfo import ZoneInfo

from system.automation.repositories import (
    AutomationPolicyRepository,
    JobDefinitionRepository,
    _automation_base_dir,
)
from system.automation.runtime import get_runtime
from system.automation.types import AutomationPolicy, JobDefinition
from agent_core.config import get_config

from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult


class SyncSourcesTool(BaseTool):
    @property
    def name(self) -> str:
        return "sync_sources"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="手动触发外部来源同步（课表/邮件）。",
            parameters=[
                ToolParameter(
                    name="source",
                    type="string",
                    description="来源：course | email | all",
                    required=False,
                ),
                ToolParameter(
                    name="account_id",
                    type="string",
                    description="账户 ID，默认 default",
                    required=False,
                ),
            ],
            tags=["自动化", "同步"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        source = str(kwargs.get("source") or "all").strip().lower()
        account_id = str(kwargs.get("account_id") or "default")
        sources = ["course", "email"] if source == "all" else [source]

        runtime = await get_runtime()

        results = []
        for source_type in sources:
            result = await runtime.sync_service.run_source(
                source_type=source_type, account_id=account_id
            )
            results.append(result)

        return ToolResult(
            success=True,
            message=f"同步完成，共处理 {len(results)} 个来源",
            data={"results": results},
        )


class GetSyncStatusTool(BaseTool):
    @property
    def name(self) -> str:
        return "get_sync_status"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="查看同步游标与最近作业运行状态。",
            parameters=[
                ToolParameter(
                    name="limit",
                    type="integer",
                    description="返回数量，默认 10",
                    required=False,
                ),
            ],
            tags=["自动化", "同步", "状态"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        limit = int(kwargs.get("limit") or 10)
        runtime = await get_runtime()
        runs = runtime.run_repo.list_recent(limit=limit)
        cursors = runtime.cursor_repo.get_all()

        return ToolResult(
            success=True,
            message="已获取同步状态",
            data={
                "runs": [run.model_dump(mode="json") for run in runs],
                "cursors": [cursor.model_dump(mode="json") for cursor in cursors],
            },
        )


class GetDigestTool(BaseTool):
    @property
    def name(self) -> str:
        return "get_digest"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="查询日结/周结摘要，若不存在可触发生成。",
            parameters=[
                ToolParameter(
                    name="digest_type",
                    type="string",
                    description="daily | weekly",
                    required=False,
                ),
                ToolParameter(
                    name="generate_if_missing",
                    type="boolean",
                    description="缺失时是否生成",
                    required=False,
                ),
            ],
            tags=["自动化", "总结"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        digest_type = str(kwargs.get("digest_type") or "daily")
        generate_if_missing = bool(kwargs.get("generate_if_missing", True))

        runtime = await get_runtime()
        digest = runtime.digest_repo.latest(digest_type)
        if digest is None and generate_if_missing:
            if digest_type == "weekly":
                digest = runtime.summary_service.generate_weekly_digest()
                await runtime.bus.publish(
                    "weekly_digest.ready", {"digest_id": digest.id}
                )
            else:
                digest = runtime.summary_service.generate_daily_digest()
                await runtime.bus.publish(
                    "daily_digest.ready", {"digest_id": digest.id}
                )

        if digest is None:
            return ToolResult(success=True, message="暂无摘要", data={"digest": None})

        return ToolResult(
            success=True,
            message="已获取摘要",
            data={"digest": digest.model_dump(mode="json")},
        )


class ListNotificationsTool(BaseTool):
    @property
    def name(self) -> str:
        return "list_notifications"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="列出自动化通知（默认应用内通知）。",
            parameters=[
                ToolParameter(
                    name="limit",
                    type="integer",
                    description="返回数量，默认 20",
                    required=False,
                ),
                ToolParameter(
                    name="status",
                    type="string",
                    description="pending|sent|acked|failed",
                    required=False,
                ),
            ],
            tags=["自动化", "通知"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        limit = int(kwargs.get("limit") or 20)
        status = kwargs.get("status")

        runtime = await get_runtime()
        notifications = runtime.notification_service.list_notifications(
            limit=limit, status=status
        )
        return ToolResult(
            success=True,
            message=f"返回 {len(notifications)} 条通知",
            data={
                "notifications": [
                    item.model_dump(mode="json") for item in notifications
                ]
            },
        )


class AckNotificationTool(BaseTool):
    @property
    def name(self) -> str:
        return "ack_notification"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="确认已读一条通知。",
            parameters=[
                ToolParameter(
                    name="outbox_id",
                    type="string",
                    description="通知 ID",
                    required=True,
                ),
            ],
            tags=["自动化", "通知"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        outbox_id = str(kwargs.get("outbox_id") or "").strip()
        if not outbox_id:
            return ToolResult(
                success=False, error="MISSING_ID", message="缺少 outbox_id"
            )

        runtime = await get_runtime()
        item = runtime.notification_service.ack_notification(outbox_id)
        if item is None:
            return ToolResult(
                success=False, error="NOT_FOUND", message=f"通知不存在: {outbox_id}"
            )

        return ToolResult(
            success=True,
            message="通知已确认",
            data={"notification": item.model_dump(mode="json")},
        )


class ConfigureAutomationPolicyTool(BaseTool):
    def __init__(self, base_dir: Optional[str] = None):
        self._repo = AutomationPolicyRepository(base_dir=base_dir)

    @property
    def name(self) -> str:
        return "configure_automation_policy"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="配置自动化策略，例如自动写入开关和静默时段。",
            parameters=[
                ToolParameter(
                    name="auto_write_enabled",
                    type="boolean",
                    description="是否启用自动写入",
                    required=False,
                ),
                ToolParameter(
                    name="quiet_hours_start",
                    type="string",
                    description="静默开始时间 HH:MM",
                    required=False,
                ),
                ToolParameter(
                    name="quiet_hours_end",
                    type="string",
                    description="静默结束时间 HH:MM",
                    required=False,
                ),
                ToolParameter(
                    name="min_confidence_for_silent_apply",
                    type="number",
                    description="静默自动应用置信度阈值",
                    required=False,
                ),
            ],
            tags=["自动化", "策略"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        policy = self._repo.get_default()

        if "auto_write_enabled" in kwargs and kwargs["auto_write_enabled"] is not None:
            policy.auto_write_enabled = bool(kwargs["auto_write_enabled"])
        if kwargs.get("quiet_hours_start") is not None:
            policy.quiet_hours_start = str(kwargs["quiet_hours_start"])
        if kwargs.get("quiet_hours_end") is not None:
            policy.quiet_hours_end = str(kwargs["quiet_hours_end"])
        if kwargs.get("min_confidence_for_silent_apply") is not None:
            policy.min_confidence_for_silent_apply = float(
                kwargs["min_confidence_for_silent_apply"]
            )

        policy.updated_at = datetime.now()
        self._repo.update(policy)

        # 兼容首次创建后 update 失败的场景
        if self._repo.get(policy.id) is None:
            self._repo.create(AutomationPolicy(**policy.model_dump()))

        return ToolResult(
            success=True,
            message="自动化策略已更新",
            data={"policy": policy.model_dump(mode="json")},
        )


class GetAutomationActivityTool(BaseTool):
    @property
    def name(self) -> str:
        return "get_automation_activity"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="查看最近的自动化任务活动简报（操作 + 结果）。",
            parameters=[
                ToolParameter(
                    name="limit",
                    type="integer",
                    description="返回最近多少条记录，默认 20",
                    required=False,
                ),
            ],
            tags=["自动化", "日志", "活动"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        limit = int(kwargs.get("limit") or 20)
        base_dir = _automation_base_dir()
        path = base_dir / "automation_activity.jsonl"
        activities: list[dict[str, Any]] = []

        if path.exists():
            try:
                # 简单实现：读取全部行后取最后 N 条，考虑到文件规模较小。
                lines = path.read_text(encoding="utf-8").splitlines()
                for line in lines[-max(1, limit) :]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        activities.append(json.loads(line))
                    except Exception:
                        # 忽略单条损坏记录
                        continue
            except Exception:
                # 任何读取错误时返回空列表而不是抛出异常，避免影响对话体验。
                activities = []

        return ToolResult(
            success=True,
            message=f"共返回 {len(activities)} 条自动化活动简报",
            data={"activities": activities},
        )


class NotifyOwnerTool(BaseTool):
    """向主人发送飞书通知。需配置 feishu.automation_activity_enabled=true 和 automation_activity_chat_id。"""

    def __init__(self, config: Optional[Any] = None):
        self._config = config or get_config()

    @property
    def name(self) -> str:
        return "notify_owner"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""向主人发送飞书消息通知。

使用场景：
- 遇到政治敏感、合规风险等问题礼貌拒绝后，主动通知主人
- 需要主人知晓的重要事件或异常情况

消息会发送到配置的 feishu.automation_activity_chat_id 所指的飞书会话。需配置 feishu.automation_activity_enabled=true 且 automation_activity_chat_id 非空。""",
            parameters=[
                ToolParameter(
                    name="message",
                    type="string",
                    description="要通知主人的消息内容",
                    required=True,
                ),
            ],
            usage_notes=["若未配置飞书通知目标 chat_id，本工具会返回友好提示"],
            tags=["飞书", "通知", "主人"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        message = str(kwargs.get("message") or "").strip()
        if not message:
            return ToolResult(
                success=False,
                error="MISSING_MESSAGE",
                message="请提供要通知的内容 message",
            )

        feishu_cfg = getattr(self._config, "feishu", None) or {}
        enabled = getattr(feishu_cfg, "automation_activity_enabled", False)
        chat_id = getattr(feishu_cfg, "automation_activity_chat_id", "") or ""

        if not enabled or not chat_id:
            return ToolResult(
                success=False,
                error="FEISHU_NOTIFY_NOT_CONFIGURED",
                message="未配置飞书通知：请在 config.yaml 中设置 feishu.automation_activity_enabled=true 和 feishu.automation_activity_chat_id（接收通知的飞书 chat_id）",
            )

        try:
            from frontend.feishu.client import FeishuClient
            from frontend.feishu.reply_dispatch import send_feishu_agent_reply
        except ImportError as e:
            return ToolResult(
                success=False,
                error="FEISHU_IMPORT_ERROR",
                message=f"无法导入飞书客户端: {e}",
            )

        try:
            feishu_to = float(getattr(feishu_cfg, "timeout_seconds", None) or 30.0)
            client = FeishuClient(timeout_seconds=max(feishu_to, 120.0))
            await send_feishu_agent_reply(
                client=client,
                chat_id=chat_id,
                output_text=message,
                markdown_card_header_title="通知",
                reply_phase="final",
            )
            return ToolResult(
                success=True, message="已发送飞书通知", data={"sent": True}
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error="FEISHU_SEND_FAILED",
                message=f"发送飞书通知失败: {e}",
            )


class CreateScheduledJobTool(BaseTool):
    def __init__(
        self,
        base_dir: Optional[str] = None,
        *,
        default_memory_owner: Optional[str] = None,
        default_core_mode: Optional[str] = None,
        default_tool_template: Optional[str] = None,
    ):
        self._repo = JobDefinitionRepository(base_dir=base_dir)
        self._default_memory_owner = default_memory_owner
        self._default_core_mode = default_core_mode
        self._default_tool_template = default_tool_template

    @property
    def name(self) -> str:
        return "create_scheduled_job"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "创建一个新的定时自动化任务（job_type 固定为 agent，表示由当前 Agent 创建）。\n\n"
                "典型用法：当用户用自然语言描述“每隔多久做什么事情”或“每天几点/几点做什么事情”时，"
                "使用本工具将其注册为后台定时任务，由 automation_daemon 在后台以 ephemeral 会话周期性执行。\n\n"
                "支持四种主要语义：\n"
                "0）one-shot alarm：run_at（或 once_at）指定一次性触发时间；\n"
                "1）interval：基于 interval_minutes/interval_seconds 的固定间隔执行；\n"
                "2）daily_time/times：基于每天一个或多个固定时间点（HH:MM，本地时区）执行；\n"
                "3）start_time + interval：从某个起始时间开始，按给定间隔滚动触发。"
            ),
            parameters=[
                ToolParameter(
                    name="instruction",
                    type="string",
                    description="定时任务触发时给 Agent 的自然语言指令，例如“请调用 sync_sources(source='email') 并输出操作+结果”。",
                    required=True,
                ),
                ToolParameter(
                    name="run_at",
                    type="string",
                    description="可选：一次性触发时间（ISO-8601）。示例：2026-03-09T21:30:00+08:00。设置后该任务触发一次即自动停用。",
                    required=False,
                ),
                ToolParameter(
                    name="once_at",
                    type="string",
                    description="run_at 的别名（兼容参数）。",
                    required=False,
                ),
                ToolParameter(
                    name="interval_minutes",
                    type="integer",
                    description="任务执行间隔（分钟）。可与 interval_seconds 二选一，若都提供则以 interval_seconds 为准。",
                    required=False,
                ),
                ToolParameter(
                    name="interval_seconds",
                    type="integer",
                    description="任务执行间隔（秒）。优先于 interval_minutes。",
                    required=False,
                ),
                ToolParameter(
                    name="daily_time",
                    type="string",
                    description="可选：每天固定触发时间（HH:MM，采用配置中的 time.timezone）。设置后语义为每天这个时间点执行一次。",
                    required=False,
                ),
                ToolParameter(
                    name="times",
                    type="string",
                    description='可选：每天多个触发时间，逗号分隔的 HH:MM 列表，例如 "08:00,14:00,20:00"。设置后优先于 daily_time。',
                    required=False,
                ),
                ToolParameter(
                    name="start_time",
                    type="string",
                    description="可选：起始时间（HH:MM），与 interval_minutes/interval_seconds 搭配，表示“从此时起按间隔滚动触发”。",
                    required=False,
                ),
                ToolParameter(
                    name="job_name",
                    type="string",
                    description="必填：任务唯一名（存库主键），应简短稳定。",
                    required=True,
                ),
                ToolParameter(
                    name="user_id",
                    type="string",
                    description="逻辑用户 ID，用于区分不同用户的后台任务，默认 default。",
                    required=False,
                ),
                ToolParameter(
                    name="enabled",
                    type="boolean",
                    description="是否启用该任务，默认 true。",
                    required=False,
                ),
                ToolParameter(
                    name="memory_owner",
                    type="string",
                    description='可选：记忆 owner 标识，例如 "feishu:ou_xxx" 或 "cli:root"。不提供时与当前会话的权限对齐（有记忆则复用，无则不开）。',
                    required=False,
                ),
                ToolParameter(
                    name="core_mode",
                    type="string",
                    description="可选：Core 运行模式：full / sub / background。不提供时默认采用 full（若当前会话是 sub/background，也会自动提升为 full）。",
                    required=False,
                ),
                ToolParameter(
                    name="tool_template",
                    type="string",
                    description="可选：工具模板名，例如 default / cron / shuiyuan。不提供时由调度端按 Core 类型推导。",
                    required=False,
                ),
                ToolParameter(
                    name="remote_login",
                    type="string",
                    description="可选：远程工作区登录别名（等同 /remote-use 的 login）。配置后任务执行前会尝试绑定远程 worker。",
                    required=False,
                ),
                ToolParameter(
                    name="remote_path",
                    type="string",
                    description="可选：远程工作区路径（等同 /remote-use 的 path），默认 ~。",
                    required=False,
                ),
                ToolParameter(
                    name="remote_profile",
                    type="string",
                    description="可选：远程权限档位 strict/dev/host-user/host-admin，默认 dev。",
                    required=False,
                ),
                ToolParameter(
                    name="remote_ttl_seconds",
                    type="integer",
                    description="可选：远程工作区租约 TTL（秒）。",
                    required=False,
                ),
                ToolParameter(
                    name="remote_required",
                    type="boolean",
                    description="远程绑定失败时是否直接失败。默认 true；false 时回落到本地执行。",
                    required=False,
                ),
            ],
            tags=["自动化", "定时任务", "调度"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        exec_ctx = kwargs.pop("__execution_context__", None) or {}

        instruction = str(kwargs.get("instruction") or "").strip()
        if not instruction:
            return ToolResult(
                success=False,
                error="MISSING_INSTRUCTION",
                message="缺少定时任务指令（instruction）。",
            )

        job_name = str(kwargs.get("job_name") or "").strip()
        if not job_name:
            return ToolResult(
                success=False,
                error="MISSING_JOB_NAME",
                message="缺少任务唯一名 job_name（存库主键，必填）。",
            )

        interval_seconds_raw = kwargs.get("interval_seconds")
        interval_minutes_raw = kwargs.get("interval_minutes")
        daily_time_raw = kwargs.get("daily_time")
        times_raw = kwargs.get("times")
        start_time_raw = kwargs.get("start_time")
        run_at_raw = kwargs.get("run_at")
        once_at_raw = kwargs.get("once_at")

        run_at: Optional[datetime] = None
        run_at_text = str(run_at_raw or once_at_raw or "").strip()
        if run_at_text:
            try:
                parsed_run_at = datetime.fromisoformat(
                    run_at_text.replace("Z", "+00:00")
                )
                run_at = parsed_run_at
            except Exception:
                return ToolResult(
                    success=False,
                    error="INVALID_RUN_AT",
                    message="run_at/once_at 必须是合法的 ISO-8601 时间，例如 2026-03-09T21:30:00+08:00。",
                )

        interval_seconds: Optional[int] = None
        # daily_time / times / start_time 语义配置
        daily_time: Optional[str] = None
        if daily_time_raw:
            daily_time = str(daily_time_raw).strip() or None

        times: list[str] = []
        if times_raw:
            if isinstance(times_raw, list):
                times = [str(t).strip() for t in times_raw if str(t).strip()]
            else:
                times = [s.strip() for s in str(times_raw).split(",") if s.strip()]

        start_time: Optional[str] = None
        if start_time_raw:
            start_time = str(start_time_raw).strip() or None

        one_shot = run_at is not None
        if one_shot and any(
            [interval_seconds_raw, interval_minutes_raw, daily_time, times, start_time]
        ):
            return ToolResult(
                success=False,
                error="MIXED_SCHEDULE_MODE",
                message="run_at/once_at 为一次性闹钟模式，不能与 interval/daily_time/times/start_time 混用。",
            )

        # 解析 interval_seconds（仅循环模式）
        if not one_shot:
            if interval_seconds_raw is not None:
                try:
                    interval_seconds = int(interval_seconds_raw)
                except (TypeError, ValueError):
                    return ToolResult(
                        success=False,
                        error="INVALID_INTERVAL_SECONDS",
                        message="interval_seconds 必须是正整数（秒）。",
                    )
            elif interval_minutes_raw is not None:
                try:
                    minutes = int(interval_minutes_raw)
                except (TypeError, ValueError):
                    return ToolResult(
                        success=False,
                        error="INVALID_INTERVAL_MINUTES",
                        message="interval_minutes 必须是正整数（分钟）。",
                    )
                interval_seconds = minutes * 60

            # times / daily_time 模式未显式提供间隔时，默认按 24 小时周期。
            if (times or daily_time) and (
                interval_seconds is None or interval_seconds <= 0
            ):
                interval_seconds = 24 * 3600

            # start_time + interval 语义需要有效的 interval
            if start_time is not None and (
                interval_seconds is None or interval_seconds <= 0
            ):
                return ToolResult(
                    success=False,
                    error="MISSING_INTERVAL_FOR_START_TIME",
                    message="使用 start_time 时必须提供 interval_minutes 或 interval_seconds，且为正数。",
                )

            # 完全没有任何时间语义且没有间隔
            if interval_seconds is None or interval_seconds <= 0:
                return ToolResult(
                    success=False,
                    error="MISSING_INTERVAL",
                    message="必须至少提供 interval_minutes 或 interval_seconds，或设置 daily_time/times/start_time，或使用 run_at/once_at。",
                )
        else:
            # one-shot 模式下保持 interval_seconds 的最小合法值（不会参与下一轮调度）
            interval_seconds = 1

        user_id = str(kwargs.get("user_id") or "default")
        enabled = bool(kwargs.get("enabled", True))
        # 未显式传入时，使用调用此工具的 Core 的权限作为默认
        memory_owner = (
            str(kwargs.get("memory_owner") or "").strip() or self._default_memory_owner
        )
        explicit_core_mode = str(kwargs.get("core_mode") or "").strip()
        if explicit_core_mode:
            core_mode = explicit_core_mode
        else:
            inherited_mode = str(self._default_core_mode or "").strip().lower()
            if inherited_mode in {"sub", "background", "cron", "heartbeat"}:
                core_mode = "full"
            else:
                core_mode = self._default_core_mode
        tool_template = (
            str(kwargs.get("tool_template") or "").strip() or self._default_tool_template
        )
        feishu_chat_id = str(exec_ctx.get("feishu_chat_id") or "").strip() or None
        remote_login = str(kwargs.get("remote_login") or "").strip() or None
        remote_path = str(kwargs.get("remote_path") or "").strip() or "~"
        remote_profile = str(kwargs.get("remote_profile") or "").strip().lower() or "dev"
        if remote_login is None:
            session_id = str(exec_ctx.get("session_id") or "").strip()
            if session_id:
                try:
                    from agent_core.remote.workspace_state import (
                        get_remote_workspace_state,
                    )

                    remote_state = get_remote_workspace_state(session_id)
                except Exception:
                    remote_state = None
                if remote_state is not None:
                    remote_login = (remote_state.login or "").strip() or None
                    if remote_login:
                        remote_path = (
                            (remote_state.requested_path or remote_state.resolved_path or "~")
                            .strip()
                            or "~"
                        )
                        remote_profile = (
                            str(remote_state.profile or "dev").strip().lower() or "dev"
                        )
        if remote_profile not in {"strict", "dev", "host-user", "host-admin"}:
            return ToolResult(
                success=False,
                error="INVALID_REMOTE_PROFILE",
                message="remote_profile 必须是 strict/dev/host-user/host-admin 之一。",
            )
        remote_ttl_raw = kwargs.get("remote_ttl_seconds")
        remote_ttl_seconds: Optional[int] = None
        if remote_ttl_raw is not None:
            try:
                remote_ttl_seconds = int(remote_ttl_raw)
            except (TypeError, ValueError):
                return ToolResult(
                    success=False,
                    error="INVALID_REMOTE_TTL",
                    message="remote_ttl_seconds 必须是正整数秒。",
                )
            if remote_ttl_seconds <= 0:
                return ToolResult(
                    success=False,
                    error="INVALID_REMOTE_TTL",
                    message="remote_ttl_seconds 必须是正整数秒。",
                )
        remote_required = bool(kwargs.get("remote_required", True))

        try:
            cfg = get_config()
            timezone = cfg.time.timezone
        except Exception:
            # 回退时也统一使用上海时区，避免混用 UTC。
            timezone = "Asia/Shanghai"

        job = JobDefinition(
            job_name=job_name,
            job_type="agent",
            enabled=enabled,
            one_shot=one_shot,
            run_at=run_at,
            interval_seconds=interval_seconds,
            timezone=timezone,
            payload_template={
                "instruction": instruction,
                "user_id": user_id,
                **({"daily_time": daily_time} if daily_time is not None else {}),
                **({"times": times} if times else {}),
                **({"start_time": start_time} if start_time is not None else {}),
                **({"memory_owner": memory_owner} if memory_owner is not None else {}),
                **({"core_mode": core_mode} if core_mode is not None else {}),
                **({"tool_template": tool_template} if tool_template is not None else {}),
                **({"feishu_chat_id": feishu_chat_id} if feishu_chat_id else {}),
                **({"remote_login": remote_login} if remote_login else {}),
                **({"remote_path": remote_path} if remote_login else {}),
                **({"remote_profile": remote_profile} if remote_login else {}),
                **(
                    {"remote_ttl_seconds": remote_ttl_seconds}
                    if remote_login and remote_ttl_seconds is not None
                    else {}
                ),
                **({"remote_required": remote_required} if remote_login else {}),
            },
        )

        self._repo.create(job)

        return ToolResult(
            success=True,
            message="定时任务已创建。",
            data={"job": job.model_dump(mode="json")},
        )


class ListScheduledJobsTool(BaseTool):
    """列出已注册的自动化定时任务（含 config 同步与 Agent 创建的 job）。"""

    def __init__(self, base_dir: Optional[str] = None):
        self._repo = JobDefinitionRepository(base_dir=base_dir)

    @property
    def name(self) -> str:
        return "list_scheduled_jobs"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "列出当前 JobDefinition 仓库中的定时任务（主键为 job_name）。\n\n"
                "用于回答“有哪些定时任务”“查看后台任务配置”等；"
                "删除请用 delete_scheduled_job。"
            ),
            parameters=[
                ToolParameter(
                    name="enabled_only",
                    type="boolean",
                    description="为 true 时仅返回 enabled=true 的任务",
                    required=False,
                ),
                ToolParameter(
                    name="limit",
                    type="integer",
                    description="最多返回条数，默认 100",
                    required=False,
                ),
            ],
            tags=["自动化", "定时任务", "调度"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        kwargs.pop("__execution_context__", None)
        enabled_only = bool(kwargs.get("enabled_only", False))
        limit_raw = kwargs.get("limit")
        limit = 100
        if limit_raw is not None:
            try:
                limit = max(1, int(limit_raw))
            except (TypeError, ValueError):
                return ToolResult(
                    success=False,
                    error="INVALID_LIMIT",
                    message="limit 必须是正整数。",
                )

        items = self._repo.get_all()
        if enabled_only:
            items = [j for j in items if j.enabled]
        items.sort(key=lambda j: j.updated_at, reverse=True)
        items = items[:limit]

        return ToolResult(
            success=True,
            message=f"共 {len(items)} 条定时任务（已按更新时间倒序）。",
            data={
                "jobs": [j.model_dump(mode="json") for j in items],
                "total_returned": len(items),
            },
        )


class ScheduleWakeTool(BaseTool):
    """在当前会话登记定时唤醒，到点后通过 inject_turn 主动继续对话。"""

    @property
    def name(self) -> str:
        return "schedule_wake"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "为**当前会话**设置定时唤醒（闹钟/倒计时），到点后 daemon 会通过 inject_turn "
                "主动唤醒本会话继续处理，与 bash 后台任务完成通知走同一路径。\n\n"
                "与 create_scheduled_job 的区别：\n"
                "- schedule_wake：唤醒**当前**会话，适合「N 分钟后提醒我」「到点继续刚才的事」；\n"
                "- create_scheduled_job：在独立 cron 会话中执行指令，适合周期性后台自动化。\n\n"
                "时间语义（至少提供一种）：\n"
                "- wake_at：绝对时间（ISO-8601，如 2026-06-19T15:30:00+08:00）；\n"
                "- delay_seconds / delay_minutes：相对当前时间的倒计时。"
            ),
            parameters=[
                ToolParameter(
                    name="message",
                    type="string",
                    description="到点时注入会话的提醒/继续任务说明（必填）。",
                    required=True,
                ),
                ToolParameter(
                    name="wake_at",
                    type="string",
                    description="绝对触发时间（ISO-8601）。与 delay_* 二选一。",
                    required=False,
                ),
                ToolParameter(
                    name="delay_seconds",
                    type="integer",
                    description="倒计时秒数（正整数）。与 wake_at 二选一。",
                    required=False,
                ),
                ToolParameter(
                    name="delay_minutes",
                    type="number",
                    description="倒计时分钟数（正数）。与 wake_at 二选一。",
                    required=False,
                ),
                ToolParameter(
                    name="label",
                    type="string",
                    description="可选简短标签，便于 list_scheduled_wakes 识别。",
                    required=False,
                ),
                ToolParameter(
                    name="wake_id",
                    type="string",
                    description="可选自定义 ID；不提供则自动生成。",
                    required=False,
                ),
            ],
            usage_notes=[
                "取消已登记的唤醒请用 cancel_scheduled_wake。",
                "daemon 未运行时唤醒不会触发。",
            ],
            tags=["自动化", "定时", "唤醒"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        exec_ctx = kwargs.pop("__execution_context__", None) or {}
        session_id = str(exec_ctx.get("session_id") or "").strip()
        if not session_id:
            return ToolResult(
                success=False,
                error="MISSING_SESSION",
                message="无法识别当前会话（缺少 session_id）。",
            )

        message = str(kwargs.get("message") or "").strip()
        if not message:
            return ToolResult(
                success=False,
                error="MISSING_MESSAGE",
                message="请提供到点时的提醒内容 message。",
            )

        wake_at_raw = str(kwargs.get("wake_at") or "").strip()
        delay_seconds_raw = kwargs.get("delay_seconds")
        delay_minutes_raw = kwargs.get("delay_minutes")

        has_absolute = bool(wake_at_raw)
        has_relative = delay_seconds_raw is not None or delay_minutes_raw is not None
        if has_absolute == has_relative:
            return ToolResult(
                success=False,
                error="INVALID_TIME",
                message="请提供 wake_at（绝对时间）或 delay_seconds/delay_minutes（倒计时）之一，且不能同时提供。",
            )

        fire_at: float
        fire_at_display: str
        if has_absolute:
            try:
                parsed = datetime.fromisoformat(wake_at_raw.replace("Z", "+00:00"))
            except Exception:
                return ToolResult(
                    success=False,
                    error="INVALID_WAKE_AT",
                    message="wake_at 必须是合法的 ISO-8601 时间。",
                )
            if parsed.tzinfo is None:
                try:
                    cfg = get_config()
                    tz_name = cfg.time.timezone
                except Exception:
                    tz_name = "Asia/Shanghai"
                parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
            fire_at = parsed.timestamp()
            fire_at_display = parsed.isoformat()
        else:
            delay_seconds: float = 0.0
            if delay_seconds_raw is not None:
                try:
                    delay_seconds = float(delay_seconds_raw)
                except (TypeError, ValueError):
                    return ToolResult(
                        success=False,
                        error="INVALID_DELAY",
                        message="delay_seconds 必须是正数。",
                    )
            elif delay_minutes_raw is not None:
                try:
                    delay_seconds = float(delay_minutes_raw) * 60.0
                except (TypeError, ValueError):
                    return ToolResult(
                        success=False,
                        error="INVALID_DELAY",
                        message="delay_minutes 必须是正数。",
                    )
            if delay_seconds <= 0:
                return ToolResult(
                    success=False,
                    error="INVALID_DELAY",
                    message="倒计时必须大于 0。",
                )
            fire_at = time.time() + delay_seconds
            fire_at_display = datetime.fromtimestamp(fire_at).isoformat()

        label = str(kwargs.get("label") or "").strip()
        wake_id = str(kwargs.get("wake_id") or "").strip() or None
        metadata: dict[str, Any] = {}
        feishu_chat_id = str(exec_ctx.get("feishu_chat_id") or "").strip()
        if not feishu_chat_id and session_id.startswith("feishu:"):
            try:
                from agent_core.tools.bash_job_notify import get_notify_dependencies
                from frontend.feishu.feishu_turn_hooks import (
                    resolve_feishu_chat_id_for_session,
                )

                deps = get_notify_dependencies()
                feishu_chat_id = (
                    resolve_feishu_chat_id_for_session(
                        session_id, core_pool=deps.get("core_pool")
                    )
                    or ""
                ).strip()
            except Exception:
                feishu_chat_id = ""
        if feishu_chat_id:
            metadata["feishu_chat_id"] = feishu_chat_id

        from agent_core.tools.agent_wake import (
            cancel_pending_wakes,
            list_wakes,
            register_wake,
        )

        if wake_id:
            existing = [w for w in list_wakes() if w.get("wake_id") == wake_id]
            if existing:
                return ToolResult(
                    success=False,
                    error="DUPLICATE_WAKE_ID",
                    message=f"wake_id {wake_id!r} 已存在，请换一个或先 cancel_scheduled_wake。",
                )

        try:
            assigned_id = register_wake(
                session_id=session_id,
                fire_at=fire_at,
                message=message,
                wake_id=wake_id,
                label=label,
                frontend_id=str(exec_ctx.get("frontend_id") or "cli"),
                source=str(exec_ctx.get("source") or "cli"),
                user_id=str(exec_ctx.get("user_id") or "root"),
                metadata=metadata,
            )
            # Agent 主动 schedule_wake 时，取消 goal-check 系统续跑，避免与定时唤醒冲突
            if str(label or "").strip() != "goal-check":
                cancel_pending_wakes(session_id, label="goal-check")
        except ValueError as exc:
            return ToolResult(success=False, error="REGISTER_FAILED", message=str(exc))

        seconds_until = max(0.0, fire_at - time.time())
        return ToolResult(
            success=True,
            message=(
                f"已登记定时唤醒，约 {seconds_until:.0f} 秒后触发（{fire_at_display}）。"
            ),
            data={
                "wake_id": assigned_id,
                "session_id": session_id,
                "fire_at": fire_at,
                "fire_at_display": fire_at_display,
                "seconds_until": seconds_until,
                "label": label,
                "message": message,
            },
        )


class ListScheduledWakesTool(BaseTool):
    @property
    def name(self) -> str:
        return "list_scheduled_wakes"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="列出当前会话已登记、尚未触发的定时唤醒。",
            parameters=[],
            tags=["自动化", "定时", "唤醒"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        exec_ctx = kwargs.pop("__execution_context__", None) or {}
        session_id = str(exec_ctx.get("session_id") or "").strip()
        if not session_id:
            return ToolResult(
                success=False,
                error="MISSING_SESSION",
                message="无法识别当前会话。",
            )
        from agent_core.tools.agent_wake import list_wakes

        items = list_wakes(session_id=session_id)
        return ToolResult(
            success=True,
            message=f"当前会话有 {len(items)} 条待触发唤醒。",
            data={"wakes": items, "count": len(items)},
        )


class CancelScheduledWakeTool(BaseTool):
    @property
    def name(self) -> str:
        return "cancel_scheduled_wake"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="取消一条已登记的会话定时唤醒。",
            parameters=[
                ToolParameter(
                    name="wake_id",
                    type="string",
                    description="schedule_wake 返回的 wake_id",
                    required=True,
                ),
            ],
            tags=["自动化", "定时", "唤醒"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        kwargs.pop("__execution_context__", None)
        wake_id = str(kwargs.get("wake_id") or "").strip()
        if not wake_id:
            return ToolResult(
                success=False,
                error="MISSING_WAKE_ID",
                message="请提供 wake_id。",
            )
        from agent_core.tools.agent_wake import cancel_wake

        ok = cancel_wake(wake_id)
        if not ok:
            return ToolResult(
                success=False,
                error="NOT_FOUND",
                message=f"未找到唤醒 {wake_id!r}（可能已触发或已取消）。",
            )
        return ToolResult(
            success=True,
            message=f"已取消定时唤醒 {wake_id!r}。",
            data={"wake_id": wake_id, "cancelled": True},
        )


class DeleteScheduledJobTool(BaseTool):
    """按 job_name 从仓库删除定时任务；automation_daemon 会通过 watcher 取消调度。"""

    def __init__(self, base_dir: Optional[str] = None):
        self._repo = JobDefinitionRepository(base_dir=base_dir)

    @property
    def name(self) -> str:
        return "delete_scheduled_job"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "按 job_name 删除一条定时任务记录（从 job_definitions 持久化中移除）。\n\n"
                "若只想暂停而不删记录，可对同名任务调用 create_scheduled_job 并设 enabled=false。\n\n"
                "删除后 automation_daemon 侧会停止对该 job 的调度。"
            ),
            parameters=[
                ToolParameter(
                    name="job_name",
                    type="string",
                    description="要删除的任务唯一名（与 create_scheduled_job 的 job_name 一致）",
                    required=True,
                ),
            ],
            tags=["自动化", "定时任务", "调度"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        kwargs.pop("__execution_context__", None)
        job_name = str(kwargs.get("job_name") or "").strip()
        if not job_name:
            return ToolResult(
                success=False,
                error="MISSING_JOB_NAME",
                message="缺少 job_name。",
            )

        ok = self._repo.delete(job_name)
        if not ok:
            return ToolResult(
                success=False,
                error="NOT_FOUND",
                message=f"未找到名为 {job_name!r} 的定时任务。",
                data={"job_name": job_name, "deleted": False},
            )

        return ToolResult(
            success=True,
            message=f"已删除定时任务 {job_name!r}。",
            data={"job_name": job_name, "deleted": True},
        )
