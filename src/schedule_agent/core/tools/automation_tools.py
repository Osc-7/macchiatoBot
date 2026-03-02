"""Automation control and query tools."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Optional

from schedule_agent.automation.repositories import AutomationPolicyRepository, _automation_base_dir
from schedule_agent.automation.runtime import get_runtime
from schedule_agent.automation.types import AutomationPolicy

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult


class SyncSourcesTool(BaseTool):
    @property
    def name(self) -> str:
        return "sync_sources"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="手动触发外部来源同步（课表/邮件）。",
            parameters=[
                ToolParameter(name="source", type="string", description="来源：course | email | all", required=False),
                ToolParameter(name="account_id", type="string", description="账户 ID，默认 default", required=False),
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
            result = await runtime.sync_service.run_source(source_type=source_type, account_id=account_id)
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
                ToolParameter(name="job_type", type="string", description="可选，筛选 job_type", required=False),
                ToolParameter(name="limit", type="integer", description="返回数量，默认 10", required=False),
            ],
            tags=["自动化", "同步", "状态"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        job_type = kwargs.get("job_type")
        limit = int(kwargs.get("limit") or 10)
        runtime = await get_runtime()
        runs = runtime.run_repo.list_recent(limit=limit, job_type=job_type)
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
                ToolParameter(name="digest_type", type="string", description="daily | weekly", required=False),
                ToolParameter(name="generate_if_missing", type="boolean", description="缺失时是否生成", required=False),
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
                await runtime.bus.publish("weekly_digest.ready", {"digest_id": digest.id})
            else:
                digest = runtime.summary_service.generate_daily_digest()
                await runtime.bus.publish("daily_digest.ready", {"digest_id": digest.id})

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
                ToolParameter(name="limit", type="integer", description="返回数量，默认 20", required=False),
                ToolParameter(name="status", type="string", description="pending|sent|acked|failed", required=False),
            ],
            tags=["自动化", "通知"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        limit = int(kwargs.get("limit") or 20)
        status = kwargs.get("status")

        runtime = await get_runtime()
        notifications = runtime.notification_service.list_notifications(limit=limit, status=status)
        return ToolResult(
            success=True,
            message=f"返回 {len(notifications)} 条通知",
            data={"notifications": [item.model_dump(mode="json") for item in notifications]},
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
                ToolParameter(name="outbox_id", type="string", description="通知 ID", required=True),
            ],
            tags=["自动化", "通知"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        outbox_id = str(kwargs.get("outbox_id") or "").strip()
        if not outbox_id:
            return ToolResult(success=False, error="MISSING_ID", message="缺少 outbox_id")

        runtime = await get_runtime()
        item = runtime.notification_service.ack_notification(outbox_id)
        if item is None:
            return ToolResult(success=False, error="NOT_FOUND", message=f"通知不存在: {outbox_id}")

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
                ToolParameter(name="auto_write_enabled", type="boolean", description="是否启用自动写入", required=False),
                ToolParameter(name="quiet_hours_start", type="string", description="静默开始时间 HH:MM", required=False),
                ToolParameter(name="quiet_hours_end", type="string", description="静默结束时间 HH:MM", required=False),
                ToolParameter(name="min_confidence_for_silent_apply", type="number", description="静默自动应用置信度阈值", required=False),
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
            policy.min_confidence_for_silent_apply = float(kwargs["min_confidence_for_silent_apply"])

        policy.updated_at = datetime.utcnow()
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
