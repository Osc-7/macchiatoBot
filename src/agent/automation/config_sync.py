"""Sync automation job definitions from config.yaml to job_definitions.json.

Daemon 启动时（或周期性）将 config.automation.jobs 同步到 JobDefinitionRepository，
这样在 config 里增删改任务后无需手动改 job_definitions.json，scheduler 的
_watch_job_definitions 会在一分钟内读到更新。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from agent.config import Config

from .repositories import JobDefinitionRepository
from .types import JobDefinition

logger = logging.getLogger(__name__)


def _stable_job_id(name: str, job_type: str, user_id: str) -> str:
    """为 config 来源的任务生成稳定 job_id，便于后续 upsert 更新。"""
    key = f"{name}:{job_type}:{user_id}"
    h = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"job-config-{h}"


def _config_job_to_definition(cfg: Config, job_config: Any) -> JobDefinition:
    """将 config 中的一条 automation.jobs 转为 JobDefinition。"""
    name = job_config.name
    job_type = job_config.job_type
    user_id = job_config.user_id or "default"
    timezone = cfg.time.timezone

    # interval_seconds: 有 daily_time/times 时用 24h；否则用 interval_minutes*60；默认 24h
    interval_seconds = 24 * 3600
    if job_config.interval_minutes is not None and job_config.interval_minutes >= 1:
        interval_seconds = job_config.interval_minutes * 60
    # times / daily_time 模式下 scheduler 会按闹钟算下一次触发，这里给 24h 即可
    if job_config.times or job_config.daily_time:
        interval_seconds = 24 * 3600

    payload = {
        "name": name,
        "instruction": job_config.description,
        "user_id": user_id,
    }
    if job_config.daily_time:
        payload["daily_time"] = job_config.daily_time
    if job_config.times:
        payload["times"] = [t.strip() for t in job_config.times if t and str(t).strip()]
    if job_config.start_time:
        payload["start_time"] = job_config.start_time

    job_id = _stable_job_id(name, job_type, user_id)
    return JobDefinition(
        job_id=job_id,
        job_type=job_type,
        enabled=job_config.enabled,
        interval_seconds=interval_seconds,
        timezone=timezone,
        payload_template=payload,
    )


def sync_job_definitions_from_config(
    config: Optional[Config] = None,
    job_def_repo: Optional[JobDefinitionRepository] = None,
) -> int:
    """将 config.automation.jobs 同步到 job_definitions.json（upsert）。

    对每条 config 中的 job，若 repo 里已有同 name+job_type+user_id 的任务（任意 job_id）则更新该条，
    否则用稳定 job_id（job-config-xxx）创建。不删除、不禁用 repo 里已有且不在 config 中的任务。
    """
    from agent.config import find_config_file, get_config, load_config

    if config is not None:
        cfg = config
    else:
        # 运行时定期同步时，从磁盘重新加载一次配置，支持热更新 config.yaml。
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
        key = (str(pt.get("name") or ""), item.job_type, str(pt.get("user_id") or "default"))
        if key[0]:
            if key not in existing_by_key or item.job_id.startswith("job-config-"):
                existing_by_key[key] = item

    kept_id_for_key: dict[tuple, str] = {}
    count = 0
    for job_config in jobs:
        try:
            job = _config_job_to_definition(cfg, job_config)
            name = job_config.name
            user_id = job_config.user_id or "default"
            key = (name, job.job_type, user_id)
            existing = existing_by_key.get(key)
            if existing is not None:
                job.job_id = existing.job_id
                job.created_at = existing.created_at
                repo.update(job)
                kept_id_for_key[key] = existing.job_id
                existing_by_key.pop(key, None)
            else:
                existing = repo.get(job.job_id)
                if existing is not None:
                    job.created_at = existing.created_at
                    repo.update(job)
                else:
                    repo.create(job)
                kept_id_for_key[key] = job.job_id
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("sync config job %s to job_definitions failed: %s", getattr(job_config, "name", "?"), exc)
    for item in list(repo.get_all()):
        pt = item.payload_template or {}
        k = (str(pt.get("name") or ""), item.job_type, str(pt.get("user_id") or "default"))
        # 同一 key 下如果存在多条记录，只保留本轮同步确定的那一条，其余删除，避免重复调度。
        if k in kept_id_for_key and kept_id_for_key[k] != item.job_id:
            repo.delete(item.job_id)
            continue
        # 对于由 config 派生的任务（job-config-*），如果当前 config 中已不存在对应 key，
        # 则自动将其标记为 disabled，而不是直接删除，以便保留历史运行记录。
        if item.job_id.startswith("job-config-") and k not in kept_id_for_key and item.enabled:
            item.enabled = False
            repo.update(item)
    if count:
        logger.info("synced %d job(s) from config.automation.jobs to job_definitions.json", count)
    return count
