"""Agent task message model for queue-driven automation."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class ContextPolicy(str, Enum):
    EPHEMERAL = "ephemeral"
    PERSISTENT = "persistent"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class AgentTask(BaseModel):
    task_id: str = Field(default_factory=lambda: f"task-{uuid4().hex[:10]}")
    source: str
    """
    指令来源标识，格式约定：
      - 定时任务:  cron:{job_type}
      - CLI 用户:  cli:default
      - 社交平台:  social:{platform}:{user_id}
      - API/webhook: api:{name}
    """
    user_id: str = "default"
    session_id: str
    """
    上下文隔离 key，格式约定：
      - 定时任务:  cron:{job_type}:{date}   (每次唯一，ephemeral)
      - CLI 用户:  cli:default              (固定复用，persistent)
      - 社交平台:  social:{platform}:{uid}  (固定复用，persistent)
    """
    instruction: str
    context_policy: ContextPolicy = ContextPolicy.EPHEMERAL
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @property
    def id(self) -> str:
        return self.task_id


def make_cron_task(
    job_type: str,
    instruction: str,
    *,
    user_id: str = "default",
) -> AgentTask:
    """构造一个定时任务 AgentTask（ephemeral，session_id 含日期保证唯一性）。"""
    today = date.today().isoformat()
    return AgentTask(
        source=f"cron:{job_type}",
        user_id=user_id,
        session_id=f"cron:{job_type}:{today}",
        instruction=instruction,
        context_policy=ContextPolicy.EPHEMERAL,
    )


def make_user_task(
    instruction: str,
    *,
    channel: str = "cli",
    user_id: str = "default",
) -> AgentTask:
    """构造一个用户会话 AgentTask（persistent，session_id 按渠道固定）。"""
    return AgentTask(
        source=channel,
        user_id=user_id,
        session_id=f"{channel}:{user_id}",
        instruction=instruction,
        context_policy=ContextPolicy.PERSISTENT,
    )
