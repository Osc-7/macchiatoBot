"""Tests for automation subsystem tools."""

from __future__ import annotations

import pytest

from schedule_agent.automation.agent_task import AgentTask
from schedule_agent.automation.logging_utils import AutomationTaskLogger
from schedule_agent.automation.repositories import AutomationPolicyRepository
from schedule_agent.automation.runtime import reset_runtime
from schedule_agent.core.tools.automation_tools import (
    AckNotificationTool,
    ConfigureAutomationPolicyTool,
    GetAutomationActivityTool,
    GetDigestTool,
    GetSyncStatusTool,
    ListNotificationsTool,
    SyncSourcesTool,
)


@pytest.fixture(autouse=True)
def _isolate_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHEDULE_AGENT_TEST_DATA_DIR", str(tmp_path))
    reset_runtime()
    yield
    reset_runtime()


@pytest.mark.asyncio
async def test_sync_sources_tool_creates_cursor_and_status():
    sync_tool = SyncSourcesTool()
    result = await sync_tool.execute(source="course")

    assert result.success is True
    assert result.data["results"][0]["source"] == "course"

    status_tool = GetSyncStatusTool()
    status = await status_tool.execute(limit=5)

    assert status.success is True
    assert any(c["source_type"] == "course" for c in status.data["cursors"])


@pytest.mark.asyncio
async def test_digest_and_notification_flow():
    digest_tool = GetDigestTool()
    digest_result = await digest_tool.execute(digest_type="daily", generate_if_missing=True)

    assert digest_result.success is True
    digest = digest_result.data["digest"]
    assert digest["digest_type"] == "daily"

    list_tool = ListNotificationsTool()
    notifications = await list_tool.execute(limit=10)
    assert notifications.success is True
    assert len(notifications.data["notifications"]) >= 1

    first_id = notifications.data["notifications"][0]["outbox_id"]
    ack_tool = AckNotificationTool()
    acked = await ack_tool.execute(outbox_id=first_id)

    assert acked.success is True
    assert acked.data["notification"]["status"] == "acked"


@pytest.mark.asyncio
async def test_configure_automation_policy_tool_updates_policy():
    tool = ConfigureAutomationPolicyTool()
    result = await tool.execute(
        auto_write_enabled=False,
        quiet_hours_start="23:00",
        quiet_hours_end="08:00",
        min_confidence_for_silent_apply=0.9,
    )

    assert result.success is True

    repo = AutomationPolicyRepository()
    policy = repo.get_default()
    assert policy.auto_write_enabled is False
    assert policy.quiet_hours_start == "23:00"
    assert policy.quiet_hours_end == "08:00"
    assert policy.min_confidence_for_silent_apply == pytest.approx(0.9)


def test_automation_task_logger_required_operation_validation():
    task = AgentTask(
        source="cron:summary.daily",
        session_id="cron:summary.daily:2026-03-02",
        instruction="请生成今日日程摘要",
    )
    logger = AutomationTaskLogger(task)

    # 模拟走了错误路径（没有调用 get_digest）
    logger.log_trace_event(
        {
            "type": "tool_result",
            "name": "get_events",
            "success": True,
            "message": "ok",
        }
    )
    ok, problems = logger.evaluate_required_operations()
    assert ok is False
    assert any("get_digest" in p for p in problems)


def test_automation_task_logger_accepts_call_tool_for_required_operation():
    task = AgentTask(
        source="cron:summary.weekly",
        session_id="cron:summary.weekly:2026-03-02",
        instruction="请生成本周摘要",
    )
    logger = AutomationTaskLogger(task)

    logger.log_trace_event(
        {
            "type": "tool_call",
            "tool_call_id": "tc-1",
            "name": "call_tool",
            "arguments": {"name": "get_digest", "arguments": {"digest_type": "weekly"}},
        }
    )
    logger.log_trace_event(
        {
            "type": "tool_result",
            "tool_call_id": "tc-1",
            "name": "call_tool",
            "success": True,
            "message": "已获取摘要",
        }
    )

    ok, problems = logger.evaluate_required_operations()
    assert ok is True
    assert problems == []


@pytest.mark.asyncio
async def test_get_automation_activity_tool_reads_compact_records(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULE_AGENT_TEST_DATA_DIR", str(tmp_path))
    activity_file = tmp_path / "automation" / "automation_activity.jsonl"
    activity_file.parent.mkdir(parents=True, exist_ok=True)
    activity_file.write_text(
        "\n".join(
            [
                '{"timestamp":"2026-03-02T00:00:00","task_id":"task-1","source":"cron:sync.email","status":"success","operations":[{"operation":"sync_sources","success":true,"message":"同步完成","error":null}],"result":{"success":true,"message":"ok","error":null}}',
                '{"timestamp":"2026-03-02T00:10:00","task_id":"task-2","source":"cron:summary.daily","status":"failed","operations":[{"operation":"get_digest","success":false,"message":"失败","error":"X"}],"result":{"success":false,"message":null,"error":"X"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    tool = GetAutomationActivityTool()
    result = await tool.execute(limit=1)
    assert result.success is True
    assert len(result.data["activities"]) == 1
    assert result.data["activities"][0]["task_id"] == "task-2"
