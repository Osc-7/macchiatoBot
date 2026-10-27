"""Automation domain models."""

from __future__ import annotations

from datetime import datetime, date
from enum import Enum
from typing import Any, Dict, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class NotificationStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    ACKED = "acked"
    FAILED = "failed"


class JobDefinition(BaseModel):
    """定时任务定义：job_name 为稳定主键；job_type 仅 human/agent。"""

    model_config = ConfigDict(extra="ignore")

    job_name: str = Field(
        ...,
        min_length=1,
        description="任务唯一标识（存库主键），必填；建议可读、稳定。",
    )
    job_type: Literal["human", "agent"] = Field(
        default="human",
        description="human=config/手工 JSON；agent=某 Agent Core 调用 create_scheduled_job 创建。",
    )
    enabled: bool = True
    one_shot: bool = False
    run_at: Optional[datetime] = None
    interval_seconds: int = Field(default=3600, ge=1)
    timezone: str = "Asia/Shanghai"
    payload_template: Dict[str, Any] = Field(default_factory=dict)
    schedule: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "结构化触发规则（automation.calendar.ScheduleSpec 的 dict 形态）："
            "{kind: once|interval|calendar, times, weekdays(ISO 1-7), month_days, months, "
            "interval_seconds, start_time, run_at, catch_up, until, count, exceptions}。"
            "为空时按 payload_template 的旧语义（daily_time/times/start_time/run_at）推导。"
        ),
    )
    runs_done: int = Field(
        default=0,
        ge=0,
        description=(
            "已按规则成功触发的次数，用于 schedule.count 配额（由调度器根据成功的计划时刻对账）。"
            "只改描述、次数上限、until、catch_up 或跳过日期时保留；触发时刻规则变了才从 0 重算。"
        ),
    )
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_job_definition(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        if "job_name" not in d and "job_id" in d:
            d["job_name"] = d.pop("job_id")
        jt = d.get("job_type")
        if isinstance(jt, str) and jt not in ("human", "agent"):
            d["job_type"] = "human"
        elif jt not in ("human", "agent"):
            d["job_type"] = "human"
        d.pop("automation_kind", None)
        for k in ("created_by_session_id", "created_by_source", "created_by_user_id", "origin"):
            d.pop(k, None)
        return d

    @property
    def id(self) -> str:
        return self.job_name


class JobRun(BaseModel):
    run_id: str = Field(default_factory=lambda: f"run-{uuid4().hex[:10]}")
    job_name: str
    job_type: Literal["human", "agent"] = "human"
    triggered_at: datetime = Field(default_factory=datetime.now)
    scheduled_at: Optional[datetime] = Field(
        default=None,
        description=(
            "计划触发时刻（调度器求解出的 target）。"
            "与 triggered_at 区分：前者是日历上的那一格，后者是实际开始跑的时刻。"
            "手动触发/补跑时为空。"
        ),
    )
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    status: JobStatus = JobStatus.PENDING
    error: Optional[str] = None
    metrics: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_job_run(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        if "job_name" not in d and "job_id" in d:
            d["job_name"] = d.pop("job_id")
        jt = d.get("job_type")
        if isinstance(jt, str) and jt not in ("human", "agent"):
            d["job_type"] = "human"
        d.pop("automation_kind", None)
        d.setdefault("job_type", "human")
        return d

    @property
    def id(self) -> str:
        return self.run_id


class SourceAccount(BaseModel):
    source_type: str
    account_id: str = "default"
    auth_ref: Optional[str] = None
    status: str = "active"
    updated_at: datetime = Field(default_factory=datetime.now)

    @property
    def id(self) -> str:
        return f"{self.source_type}:{self.account_id}"


class SyncCursor(BaseModel):
    source_type: str
    account_id: str = "default"
    cursor: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.now)

    @property
    def id(self) -> str:
        return f"{self.source_type}:{self.account_id}"


class ExternalItem(BaseModel):
    source_type: str
    external_id: str
    fingerprint: str
    occurred_at: datetime
    raw_payload: Dict[str, Any] = Field(default_factory=dict)
    normalized_payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)

    @property
    def id(self) -> str:
        return f"{self.source_type}:{self.external_id}"


class Digest(BaseModel):
    digest_id: str = Field(default_factory=lambda: f"digest-{uuid4().hex[:8]}")
    digest_type: Literal["daily", "weekly"]
    period_start: date
    period_end: date
    content_md: str
    highlights: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=datetime.now)

    @property
    def id(self) -> str:
        return self.digest_id


class NotificationOutbox(BaseModel):
    outbox_id: str = Field(default_factory=lambda: f"out-{uuid4().hex[:10]}")
    channel: str = "in_app"
    target: str = "default"
    template: str = "generic"
    payload: Dict[str, Any] = Field(default_factory=dict)
    status: NotificationStatus = NotificationStatus.PENDING
    retry_count: int = 0
    next_retry_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.now)
    sent_at: Optional[datetime] = None
    acked_at: Optional[datetime] = None

    @property
    def id(self) -> str:
        return self.outbox_id


class AutomationPolicy(BaseModel):
    policy_id: str = "default"
    auto_write_enabled: bool = True
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    min_confidence_for_silent_apply: float = Field(default=0.8, ge=0.0, le=1.0)
    updated_at: datetime = Field(default_factory=datetime.now)

    @property
    def id(self) -> str:
        return self.policy_id
