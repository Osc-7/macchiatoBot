from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.config import AutomationConfig, AutomationJobConfig, Config, LLMConfig
from system.automation.config_sync import sync_job_definitions_from_config
from system.automation.repositories import JobDefinitionRepository
from system.automation.types import JobDefinition


def _make_base_config(tmp_path: Path) -> Config:
    return Config(
        llm=LLMConfig(api_key="x", model="x"),
        automation=AutomationConfig(
            jobs=[],
        ),
    )


def test_sync_supports_one_shot_run_at(tmp_path: Path) -> None:
    repo = JobDefinitionRepository(base_dir=str(tmp_path / "automation"))
    cfg = _make_base_config(tmp_path)
    cfg.automation.jobs = [
        AutomationJobConfig(
            name="alarm_once",
            description="一次性提醒",
            run_at="2026-03-09T21:30:00+08:00",
            user_id="default",
            enabled=True,
        ),
    ]

    count = sync_job_definitions_from_config(config=cfg, job_def_repo=repo)
    assert count == 1
    jobs = repo.get_all()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.one_shot is True
    assert job.run_at is not None
    assert job.enabled is True
    assert job.job_type == "human"
    assert job.job_type == "human"


@pytest.mark.asyncio
async def test_sync_creates_and_disables_config_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config.jobs 新增/删除时，应创建或禁用对应的 job-config-* 任务，而不影响其他任务。"""

    # 使用独立的数据目录，避免污染真实数据
    data_dir = tmp_path / "automation"
    data_dir.mkdir(parents=True, exist_ok=True)

    # JobDefinitionRepository 使用 _automation_base_dir(base_dir)
    repo = JobDefinitionRepository(base_dir=str(data_dir))

    # 先准备一个非 config 来源的任务，后续不应被 sync 影响
    manual_job = JobDefinition(
        job_name="manual-test-1",
        job_type="human",
        enabled=True,
        payload_template={"name": "manual", "user_id": "default"},
    )
    repo.create(manual_job)

    # 第一次：config 里声明两个任务
    cfg = _make_base_config(tmp_path)
    cfg.automation.jobs = [
        AutomationJobConfig(
            name="job_a",
            description="do A",
            daily_time="08:00",
            user_id="default",
            enabled=True,
        ),
        AutomationJobConfig(
            name="job_b",
            description="do B",
            daily_time="09:00",
            user_id="default",
            enabled=True,
        ),
    ]

    count = sync_job_definitions_from_config(config=cfg, job_def_repo=repo)
    assert count == 2

    all_jobs = {j.payload_template.get("name"): j for j in repo.get_all()}
    assert "manual" in all_jobs
    assert "job_a" in all_jobs
    assert "job_b" in all_jobs
    assert all_jobs["job_a"].enabled is True
    assert all_jobs["job_b"].enabled is True

    # 记录 job_a 的 job_name，后续应保持稳定（更新而不是新建）
    job_a_id = all_jobs["job_a"].job_name

    # 第二次：从 config 中删掉 job_b，仅保留 job_a
    cfg2 = _make_base_config(tmp_path)
    cfg2.automation.jobs = [
        AutomationJobConfig(
            name="job_a",
            description="do A updated",
            daily_time="10:00",
            user_id="default",
            enabled=True,
        ),
    ]

    count2 = sync_job_definitions_from_config(config=cfg2, job_def_repo=repo)
    assert count2 == 1

    all_jobs2 = {j.payload_template.get("name"): j for j in repo.get_all()}
    # manual 仍然存在且启用
    assert "manual" in all_jobs2
    assert all_jobs2["manual"].enabled is True

    # job_a 仍然存在且启用，且 job_name 未变化
    assert "job_a" in all_jobs2
    assert all_jobs2["job_a"].job_name == job_a_id
    assert all_jobs2["job_a"].enabled is True
    # 描述/时间已更新
    assert all_jobs2["job_a"].payload_template.get("daily_time") == "10:00"
    assert all_jobs2["job_a"].payload_template.get("instruction") == "do A updated"

    # job_b 仍然存在，但应被自动标记为 disabled
    assert "job_b" in all_jobs2
    assert all_jobs2["job_b"].enabled is False
