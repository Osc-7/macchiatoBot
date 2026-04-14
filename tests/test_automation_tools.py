"""Tests for automation subsystem tools."""

from __future__ import annotations

import pytest

from system.automation.agent_task import AgentTask
from system.automation.logging_utils import AutomationTaskLogger
from system.automation.repositories import AutomationPolicyRepository
from system.automation.runtime import reset_runtime
from system.tools.automation_tools import (
    AckNotificationTool,
    ConfigureAutomationPolicyTool,
    CreateScheduledJobTool,
    DeleteScheduledJobTool,
    GetAutomationActivityTool,
    GetDigestTool,
    GetSyncStatusTool,
    ListNotificationsTool,
    ListScheduledJobsTool,
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
    digest_result = await digest_tool.execute(
        digest_type="daily", generate_if_missing=True
    )

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
async def test_get_automation_activity_tool_reads_compact_records(
    tmp_path, monkeypatch
):
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


@pytest.mark.asyncio
async def test_create_scheduled_job_supports_one_shot_alarm(tmp_path):
    tool = CreateScheduledJobTool(base_dir=str(tmp_path / "automation"))
    result = await tool.execute(
        job_name="water-reminder-once",
        instruction="到点后提醒我喝水",
        run_at="2026-03-09T21:30:00+08:00",
        user_id="u1",
    )

    assert result.success is True
    job = result.data["job"]
    assert job["one_shot"] is True
    assert job["run_at"] is not None
    assert job["enabled"] is True
    assert job["job_type"] == "agent"


@pytest.mark.asyncio
async def test_create_scheduled_job_uses_job_name(tmp_path):
    tool = CreateScheduledJobTool(base_dir=str(tmp_path / "automation"))
    result = await tool.execute(
        instruction="每 10 分钟 ping",
        interval_minutes=10,
        job_name="ping-every-10m",
    )
    assert result.success is True
    job = result.data["job"]
    assert job["job_type"] == "agent"
    assert job["job_name"] == "ping-every-10m"


@pytest.mark.asyncio
async def test_create_scheduled_job_rejects_mixed_one_shot_and_interval(tmp_path):
    tool = CreateScheduledJobTool(base_dir=str(tmp_path / "automation"))
    result = await tool.execute(
        job_name="bad-mixed-schedule",
        instruction="闹钟+循环冲突示例",
        run_at="2026-03-09T21:30:00+08:00",
        interval_minutes=5,
    )
    assert result.success is False
    assert result.error == "MIXED_SCHEDULE_MODE"


@pytest.mark.asyncio
async def test_list_and_delete_scheduled_jobs(tmp_path):
    base = str(tmp_path / "automation")
    create = CreateScheduledJobTool(base_dir=base)
    await create.execute(
        instruction="每 5 分钟 ping",
        interval_minutes=5,
        job_name="ping-test",
    )

    list_tool = ListScheduledJobsTool(base_dir=base)
    listed = await list_tool.execute()
    assert listed.success is True
    assert len(listed.data["jobs"]) == 1
    assert listed.data["jobs"][0]["job_name"] == "ping-test"

    delete_tool = DeleteScheduledJobTool(base_dir=base)
    missing = await delete_tool.execute(job_name="nope")
    assert missing.success is False
    assert missing.error == "NOT_FOUND"

    ok = await delete_tool.execute(job_name="ping-test")
    assert ok.success is True
    assert ok.data["deleted"] is True

    after = await list_tool.execute()
    assert after.success is True
    assert after.data["jobs"] == []
