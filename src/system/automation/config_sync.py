"""Sync automation job definitions from config/config.yaml to job_definitions.json.

Daemon 启动时（或周期性）将 config.automation.jobs 同步到 JobDefinitionRepository，
这样在 config 里增删改任务后无需手动改 job_definitions.json，scheduler 的
_watch_job_definitions 会在一分钟内读到更新。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Optional

from agent_core.config import Config

from .calendar import (
    ScheduleSpec,
    ScheduleSpecError,
    normalize_count,
    normalize_exceptions,
    normalize_ints,
    normalize_times,
    normalize_until,
    schedule_cadence,
)

from .repositories import JobDefinitionRepository
from .types import JobDefinition

logger = logging.getLogger(__name__)


def _stable_job_name(name: str, memory_owner: str) -> str:
    """为 config 来源的任务生成稳定 job_name，便于后续 upsert 更新。"""
    key = f"{name}:{memory_owner}"
    h = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"job-config-{h}"


def _inherit_runs_done(existing: JobDefinition, new: JobDefinition) -> int:
    """config 同步是「覆盖写」，不能把调度器记的 runs_done 抹掉。

    只有「什么时候触发」变了才从 0 重算。改描述、次数上限、until、catch_up
    或跳过日期都还是同一条规则，已跑次数留着。
    """
    if schedule_cadence(existing.schedule) != schedule_cadence(new.schedule):
        return 0
    return int(getattr(existing, "runs_done", 0) or 0)


def _config_job_to_definition(cfg: Config, job_config: Any) -> JobDefinition:
    """将 config 中的一条 automation.jobs 转为 JobDefinition。"""
    name = job_config.name
    user_id = job_config.user_id or "default"
    memory_owner = getattr(job_config, "memory_owner", None) or ""
    core_mode = getattr(job_config, "core_mode", None)
    tool_template = getattr(job_config, "tool_template", None) or ""
    remote_login = (getattr(job_config, "remote_login", None) or "").strip()
    remote_path = (getattr(job_config, "remote_path", None) or "~").strip() or "~"
    remote_profile = (
        getattr(job_config, "remote_profile", None) or "dev"
    )
    remote_ttl_seconds = getattr(job_config, "remote_ttl_seconds", None)
    remote_required = bool(getattr(job_config, "remote_required", True))
    timezone = cfg.time.timezone

    run_at: Optional[datetime] = None
    run_at_raw = getattr(job_config, "run_at", None)
    if run_at_raw:
        try:
            run_at = datetime.fromisoformat(str(run_at_raw).replace("Z", "+00:00"))
        except Exception:
            run_at = None

    one_shot = bool(getattr(job_config, "one_shot", False) or run_at is not None)

    interval_seconds = 1 if one_shot else 24 * 3600
    if (
        not one_shot
        and job_config.interval_minutes is not None
        and job_config.interval_minutes >= 1
    ):
        interval_seconds = job_config.interval_minutes * 60
    if not one_shot and (job_config.times or job_config.daily_time):
        interval_seconds = 24 * 3600

    payload = {
        "name": name,
        "instruction": job_config.description,
        "user_id": user_id,
    }
    if memory_owner:
        payload["memory_owner"] = memory_owner
    if core_mode:
        payload["core_mode"] = core_mode
    if tool_template:
        payload["tool_template"] = tool_template
    if remote_login:
        payload["remote_login"] = remote_login
        payload["remote_path"] = remote_path
        payload["remote_profile"] = str(remote_profile).strip() or "dev"
        payload["remote_required"] = remote_required
        if remote_ttl_seconds is not None:
            payload["remote_ttl_seconds"] = int(remote_ttl_seconds)
    if job_config.daily_time:
        payload["daily_time"] = job_config.daily_time
    if job_config.times:
        payload["times"] = [t.strip() for t in job_config.times if t and str(t).strip()]
    if job_config.start_time:
        payload["start_time"] = job_config.start_time
    if getattr(job_config, "write_recent_topic", False):
        payload["write_recent_topic"] = True

    stable_owner = memory_owner or user_id
    job_name = _stable_job_name(name, stable_owner)

    # 结构化触发规则（与 payload 的 daily_time/times/start_time 双写，便于回滚）
    schedule_obj = None
    try:
        catch_up = bool(getattr(job_config, "catch_up", False))
        # 有界规则（到期 / 次数上限 / 跳过某些天），对任意 kind 都可叠加。
        bounds = {
            "until": normalize_until(getattr(job_config, "until", None)),
            "count": normalize_count(getattr(job_config, "count", None)),
            "exceptions": normalize_exceptions(
                getattr(job_config, "skip_dates", None)
            ),
        }
        if one_shot and run_at is not None:
            schedule_obj = ScheduleSpec(
                kind="once", run_at=run_at, catch_up=catch_up, **bounds
            )
        else:
            calendar_times = normalize_times(
                job_config.times
                or ([job_config.daily_time] if job_config.daily_time else None)
            )
            weekdays = normalize_ints(
                getattr(job_config, "weekdays", None), 1, 7, "weekdays"
            )
            month_days = normalize_ints(
                getattr(job_config, "month_days", None), 1, 31, "month_days"
            )
            months = normalize_ints(
                getattr(job_config, "months", None), 1, 12, "months"
            )
            if calendar_times:
                schedule_obj = ScheduleSpec(
                    kind="calendar",
                    times=calendar_times,
                    weekdays=weekdays,
                    month_days=month_days,
                    months=months,
                    catch_up=catch_up,
                    **bounds,
                )
            elif job_config.start_time:
                schedule_obj = ScheduleSpec(
                    kind="interval",
                    interval_seconds=interval_seconds,
                    start_time=job_config.start_time,
                    catch_up=catch_up,
                    **bounds,
                )
            else:
                schedule_obj = ScheduleSpec(
                    kind="interval",
                    interval_seconds=interval_seconds,
                    catch_up=catch_up,
                    **bounds,
                )
    except ScheduleSpecError as exc:
        # 配置写错时不让整批同步失败：记 warning，调度侧按 legacy payload 推导。
        logger.warning("config_sync: job %s 的日历字段非法（%s），回退旧语义", name, exc)
        schedule_obj = None

    return JobDefinition(
        job_name=job_name,
        job_type="human",
        enabled=job_config.enabled,
        one_shot=one_shot,
        run_at=run_at,
        interval_seconds=interval_seconds,
        timezone=timezone,
        payload_template=payload,
        schedule=schedule_obj.to_dict() if schedule_obj is not None else None,
    )


def sync_job_definitions_from_config(
    config: Optional[Config] = None,
    job_def_repo: Optional[JobDefinitionRepository] = None,
) -> int:
    """将 config.automation.jobs 同步到 job_definitions.json（upsert）。

    对每条 config 中的 job，若 repo 里已有同 name+owner 的任务则更新该条，
    否则用稳定 job_name（job-config-xxx）创建。
    """
    from agent_core.config import find_config_file, get_config, load_config

    if config is not None:
        cfg = config
    else:
        try:
            cfg = load_config(find_config_file())
        except Exception:
            cfg = get_config()
    repo = job_def_repo or JobDefinitionRepository()
    jobs = getattr(cfg.automation, "jobs", None) or []
    if not jobs:
        return 0

    existing_by_key: dict[tuple, JobDefinition] = {}
    for item in repo.get_all():
        pt = item.payload_template or {}
        key = (
            str(pt.get("name") or ""),
            str(pt.get("memory_owner") or pt.get("user_id") or "default"),
        )
        if key[0]:
            if key not in existing_by_key or item.job_name.startswith("job-config-"):
                existing_by_key[key] = item

    kept_id_for_key: dict[tuple, str] = {}
    count = 0
    for job_config in jobs:
        try:
            job = _config_job_to_definition(cfg, job_config)
            name = job_config.name
            memory_owner = getattr(job_config, "memory_owner", None) or ""
            stable_owner = memory_owner or (job_config.user_id or "default")
            key = (name, stable_owner)
            existing = existing_by_key.get(key)
            if existing is not None:
                job.job_name = existing.job_name
                job.created_at = existing.created_at
                job.runs_done = _inherit_runs_done(existing, job)
                repo.update(job)
                kept_id_for_key[key] = existing.job_name
                existing_by_key.pop(key, None)
            else:
                existing = repo.get(job.job_name)
                if existing is not None:
                    job.created_at = existing.created_at
                    job.runs_done = _inherit_runs_done(existing, job)
                    repo.update(job)
                else:
                    repo.create(job)
                kept_id_for_key[key] = job.job_name
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sync config job %s to job_definitions failed: %s",
                getattr(job_config, "name", "?"),
                exc,
            )
    for item in list(repo.get_all()):
        pt = item.payload_template or {}
        k = (
            str(pt.get("name") or ""),
            str(pt.get("memory_owner") or pt.get("user_id") or "default"),
        )
        if k in kept_id_for_key and kept_id_for_key[k] != item.job_name:
            repo.delete(item.job_name)
            continue
        if (
            item.job_name.startswith("job-config-")
            and k not in kept_id_for_key
            and item.enabled
        ):
            item.enabled = False
            repo.update(item)
    if count:
        logger.info(
            "synced %d job(s) from config.automation.jobs to job_definitions.json",
            count,
        )
    return count
