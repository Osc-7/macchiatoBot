"""Automation repositories."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from schedule_agent.storage.json_repository import JSONRepository

from .types import (
    AutomationPolicy,
    Digest,
    ExternalItem,
    JobDefinition,
    JobRun,
    NotificationOutbox,
    NotificationStatus,
    SourceAccount,
    SyncCursor,
)


def _automation_base_dir(base_dir: Optional[str] = None) -> Path:
    if base_dir:
        return Path(base_dir)
    test_dir = os.environ.get("SCHEDULE_AGENT_TEST_DATA_DIR")
    if test_dir:
        return Path(test_dir) / "automation"
    return Path("data") / "automation"


class JobDefinitionRepository(JSONRepository[JobDefinition]):
    def __init__(self, base_dir: Optional[str] = None):
        super().__init__(_automation_base_dir(base_dir) / "job_definitions.json", JobDefinition)

    def get_enabled(self) -> List[JobDefinition]:
        return [item for item in self.get_all() if item.enabled]


class JobRunRepository(JSONRepository[JobRun]):
    def __init__(self, base_dir: Optional[str] = None):
        super().__init__(_automation_base_dir(base_dir) / "job_runs.json", JobRun)

    def list_recent(self, limit: int = 20, job_type: Optional[str] = None) -> List[JobRun]:
        items = self.get_all()
        if job_type:
            items = [item for item in items if item.job_type == job_type]
        items.sort(key=lambda i: i.triggered_at, reverse=True)
        return items[: max(1, limit)]


class SourceAccountRepository(JSONRepository[SourceAccount]):
    def __init__(self, base_dir: Optional[str] = None):
        super().__init__(_automation_base_dir(base_dir) / "source_accounts.json", SourceAccount)


class SyncCursorRepository(JSONRepository[SyncCursor]):
    def __init__(self, base_dir: Optional[str] = None):
        super().__init__(_automation_base_dir(base_dir) / "sync_cursors.json", SyncCursor)


class ExternalItemRepository(JSONRepository[ExternalItem]):
    def __init__(self, base_dir: Optional[str] = None):
        super().__init__(_automation_base_dir(base_dir) / "external_items.json", ExternalItem)

    def get_by_source(self, source_type: str, limit: int = 50) -> List[ExternalItem]:
        items = [item for item in self.get_all() if item.source_type == source_type]
        items.sort(key=lambda i: i.occurred_at, reverse=True)
        return items[: max(1, limit)]


class DigestRepository(JSONRepository[Digest]):
    def __init__(self, base_dir: Optional[str] = None):
        super().__init__(_automation_base_dir(base_dir) / "digests.json", Digest)

    def latest(self, digest_type: str) -> Optional[Digest]:
        items = [item for item in self.get_all() if item.digest_type == digest_type]
        if not items:
            return None
        items.sort(key=lambda i: i.generated_at, reverse=True)
        return items[0]


class NotificationOutboxRepository(JSONRepository[NotificationOutbox]):
    def __init__(self, base_dir: Optional[str] = None):
        super().__init__(_automation_base_dir(base_dir) / "notification_outbox.json", NotificationOutbox)

    def list_by_status(self, status: Optional[NotificationStatus] = None, limit: int = 50) -> List[NotificationOutbox]:
        items = self.get_all()
        if status is not None:
            items = [item for item in items if item.status == status]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items[: max(1, limit)]


class AutomationPolicyRepository(JSONRepository[AutomationPolicy]):
    def __init__(self, base_dir: Optional[str] = None):
        super().__init__(_automation_base_dir(base_dir) / "automation_policy.json", AutomationPolicy)

    def get_default(self) -> AutomationPolicy:
        current = self.get("default")
        if current is not None:
            return current
        policy = AutomationPolicy()
        self.create(policy)
        return policy
