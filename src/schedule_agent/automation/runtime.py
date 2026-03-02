"""Automation runtime wiring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from schedule_agent.storage.json_repository import EventRepository, TaskRepository

from .connectors import CanvasConnector, CourseConnectorStub, EmailConnectorStub
from .event_bus import AsyncEventBus
from .repositories import (
    AutomationPolicyRepository,
    DigestRepository,
    ExternalItemRepository,
    JobDefinitionRepository,
    JobRunRepository,
    NotificationOutboxRepository,
    SyncCursorRepository,
)
from .scheduler import AutomationScheduler
from .services import (
    NormalizationWriteService,
    NotificationOutboxService,
    SummaryService,
    SyncIngestionService,
)


@dataclass
class AutomationRuntime:
    bus: AsyncEventBus
    scheduler: AutomationScheduler
    sync_service: SyncIngestionService
    summary_service: SummaryService
    notification_service: NotificationOutboxService
    policy_repo: AutomationPolicyRepository
    digest_repo: DigestRepository
    cursor_repo: SyncCursorRepository
    run_repo: JobRunRepository

    async def start(self, start_scheduler: bool = False) -> None:
        self.bus.subscribe("sync.requested", self._on_sync_requested)
        self.bus.subscribe("summary.requested", self.summary_service.handle_summary_requested)
        self.bus.subscribe("daily_digest.ready", self.notification_service.handle_digest_ready)
        self.bus.subscribe("weekly_digest.ready", self.notification_service.handle_digest_ready)

        if start_scheduler:
            self.scheduler.ensure_default_jobs()
            await self.scheduler.start()

    async def stop(self) -> None:
        await self.scheduler.stop()

    async def _on_sync_requested(self, event: dict) -> None:
        payload = event.get("payload", {})
        source_type = payload.get("source_type", "course")
        account_id = payload.get("account_id", "default")
        await self.sync_service.run_source(source_type=source_type, account_id=account_id)


_runtime: Optional[AutomationRuntime] = None


async def get_runtime(base_dir: Optional[str] = None) -> AutomationRuntime:
    global _runtime
    if _runtime is None:
        bus = AsyncEventBus()
        job_def_repo = JobDefinitionRepository(base_dir=base_dir)
        run_repo = JobRunRepository(base_dir=base_dir)
        external_repo = ExternalItemRepository(base_dir=base_dir)
        cursor_repo = SyncCursorRepository(base_dir=base_dir)
        digest_repo = DigestRepository(base_dir=base_dir)
        outbox_repo = NotificationOutboxRepository(base_dir=base_dir)
        policy_repo = AutomationPolicyRepository(base_dir=base_dir)

        course_connector = CanvasConnector.from_app_config()
        connectors = [EmailConnectorStub()]
        if course_connector.is_available:
            connectors.insert(0, course_connector)
        else:
            connectors.insert(0, CourseConnectorStub())

        sync_service = SyncIngestionService(
            connectors=connectors,
            external_repo=external_repo,
            cursor_repo=cursor_repo,
            event_bus=bus,
        )

        normalization = NormalizationWriteService(
            event_repo=EventRepository(),
            task_repo=TaskRepository(),
            policy_repo=policy_repo,
            event_bus=bus,
        )
        bus.subscribe("external.item.upserted", normalization.handle_external_item)

        summary_service = SummaryService(
            digest_repo=digest_repo,
            event_repo=EventRepository(),
            task_repo=TaskRepository(),
            event_bus=bus,
        )

        notification_service = NotificationOutboxService(outbox_repo=outbox_repo)

        scheduler = AutomationScheduler(bus, job_def_repo=job_def_repo, job_run_repo=run_repo)

        _runtime = AutomationRuntime(
            bus=bus,
            scheduler=scheduler,
            sync_service=sync_service,
            summary_service=summary_service,
            notification_service=notification_service,
            policy_repo=policy_repo,
            digest_repo=digest_repo,
            cursor_repo=cursor_repo,
            run_repo=run_repo,
        )
        await _runtime.start(start_scheduler=False)

    return _runtime


def reset_runtime() -> None:
    global _runtime
    _runtime = None
