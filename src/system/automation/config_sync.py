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


def _stable_job_id(name: str, job_type: str, memory_owner: str) -> str:
    """为 config 来源的任务生成稳定 job_id，便于后续 upsert 更新。"""
    # 使用 memory_owner 作为稳定键的一部分：同一“记忆 owner”下的任务保持一致，
    # 未配置 memory_owner 时回退到 user_id 以保持向后兼容。
    key = f"{name}:{job_type}:{memory_owner}"
    h = hashlib.sha256(key.encode()).hexdigest()[:8]
    return f"job-config-{h}"


def _config_job_to_definition(cfg: Config, job_config: Any) -> JobDefinition:
    """将 config 中的一条 automation.jobs 转为 JobDefinition。"""
    name = job_config.name
    job_type = job_config.job_type
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

    # interval_seconds: one-shot 用最小合法值；其余模式沿用原策略。
    interval_seconds = 1 if one_shot else 24 * 3600
    if not one_shot and job_config.interval_minutes is not None and job_config.interval_minutes >= 1:
        interval_seconds = job_config.interval_minutes * 60
    if not one_shot and (job_config.times or job_config.daily_time):
        interval_seconds = 24 * 3600

    payload = {
        "name": name,
        "instruction": job_config.description,
        "user_id": user_id,
    }
    # 仅当配置了 memory_owner 时才写入 payload；缺省表示“不加载记忆”。
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
    job_id = _stable_job_id(name, job_type, stable_owner)
    return JobDefinition(
        job_id=job_id,
        job_type=job_type,
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

    对每条 config 中的 job，若 repo 里已有同 name+job_type+user_id 的任务（任意 job_id）则更新该条，
    否则用稳定 job_id（job-config-xxx）创建。不删除、不禁用 repo 里已有且不在 config 中的任务。
    """
    from agent_core.config import find_config_file, get_config, load_config

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
        key = (
            str(pt.get("name") or ""),
            item.job_type,
            str(pt.get("memory_owner") or pt.get("user_id") or "default"),
        )
        if key[0]:
            if key not in existing_by_key or item.job_id.startswith("job-config-"):
                existing_by_key[key] = item

    kept_id_for_key: dict[tuple, str] = {}
    count = 0
    for job_config in jobs:
        try:
            job = _config_job_to_definition(cfg, job_config)
            name = job_config.name
            memory_owner = getattr(job_config, "memory_owner", None) or ""
            stable_owner = memory_owner or (job_config.user_id or "default")
            key = (name, job.job_type, stable_owner)
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
        k = (
            str(pt.get("name") or ""),
            item.job_type,
            str(pt.get("memory_owner") or pt.get("user_id") or "default"),
        )
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
