"""Automation domain models."""

from __future__ import annotations

from datetime import datetime, date
from enum import Enum
from typing import Any, Dict, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


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
    job_id: str = Field(default_factory=lambda: f"job-{uuid4().hex[:8]}")
    job_type: str
    enabled: bool = True
    interval_seconds: int = Field(default=3600, ge=1)
    timezone: str = "UTC"
    payload_template: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def id(self) -> str:
        return self.job_id


class JobRun(BaseModel):
    run_id: str = Field(default_factory=lambda: f"run-{uuid4().hex[:10]}")
    job_id: str
    job_type: str
    triggered_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    status: JobStatus = JobStatus.PENDING
    error: Optional[str] = None
    metrics: Dict[str, Any] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.run_id


class SourceAccount(BaseModel):
    source_type: str
    account_id: str = "default"
    auth_ref: Optional[str] = None
    status: str = "active"
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def id(self) -> str:
        return f"{self.source_type}:{self.account_id}"


class SyncCursor(BaseModel):
    source_type: str
    account_id: str = "default"
    cursor: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)

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
    created_at: datetime = Field(default_factory=datetime.utcnow)

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
    generated_at: datetime = Field(default_factory=datetime.utcnow)

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
    created_at: datetime = Field(default_factory=datetime.utcnow)
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
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def id(self) -> str:
        return self.policy_id
