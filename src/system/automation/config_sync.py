"""Sync automation job definitions from config.yaml to job_definitions.json.

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

from .repositories import JobDefinitionRepository
from .types import JobDefinition

logger = logging.getLogger(__name__)


def _stable_job_name(name: str, memory_owner: str) -> str:
    """为 config 来源的任务生成稳定 job_name，便于后续 upsert 更新。"""
    key = f"{name}:{memory_owner}"
    h = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"job-config-{h}"


def _config_job_to_definition(cfg: Config, job_config: Any) -> JobDefinition:
    """将 config 中的一条 automation.jobs 转为 JobDefinition。"""
    name = job_config.name
    user_id = job_config.user_id or "default"
    memory_owner = getattr(job_config, "memory_owner", None) or ""
    core_mode = getattr(job_config, "core_mode", None)
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
    if job_config.daily_time:
        payload["daily_time"] = job_config.daily_time
    if job_config.times:
        payload["times"] = [t.strip() for t in job_config.times if t and str(t).strip()]
    if job_config.start_time:
        payload["start_time"] = job_config.start_time

    stable_owner = memory_owner or user_id
    job_name = _stable_job_name(name, stable_owner)
    return JobDefinition(
        job_name=job_name,
        job_type="human",
        enabled=job_config.enabled,
        one_shot=one_shot,
        run_at=run_at,
        interval_seconds=interval_seconds,
        timezone=timezone,
        payload_template=payload,
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
                repo.update(job)
                kept_id_for_key[key] = existing.job_name
                existing_by_key.pop(key, None)
            else:
                existing = repo.get(job.job_name)
                if existing is not None:
                    job.created_at = existing.created_at
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
