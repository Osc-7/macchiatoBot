"""Canvas connector for automation sync pipeline."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from canvas_integration import CanvasClient, CanvasConfig
from canvas_integration.models import CanvasAssignment, CanvasEvent

from schedule_agent.config import Config, get_config

from .base import BaseConnector, ConnectorFetchItem, ConnectorFetchResult


class CanvasConnector(BaseConnector):
    source_type = "course"

    def __init__(
        self,
        canvas_config: Optional[CanvasConfig],
        days_ahead: int = 60,
        include_submitted: bool = False,
        client_factory: Callable[[CanvasConfig], CanvasClient] = CanvasClient,
    ):
        self._canvas_config = canvas_config
        self._days_ahead = max(1, int(days_ahead))
        self._include_submitted = bool(include_submitted)
        self._client_factory = client_factory

    @property
    def is_available(self) -> bool:
        return self._canvas_config is not None and self._canvas_config.validate()

    @classmethod
    def from_app_config(cls) -> "CanvasConnector":
        config: Optional[Config] = None
        try:
            config = get_config()
        except Exception:
            config = None

        enabled = bool(config and config.canvas.enabled)
        api_key = (
            (config.canvas.api_key if config and config.canvas.api_key else None)
            or os.getenv("CANVAS_API_KEY")
        )
        base_url = (
            (config.canvas.base_url if config else None)
            or os.getenv("CANVAS_BASE_URL")
            or "https://oc.sjtu.edu.cn/api/v1"
        )
        days_ahead = (config.canvas.default_days_ahead if config else 60)
        include_submitted = (config.canvas.include_submitted if config else False)

        canvas_config = None
        if enabled and api_key:
            canvas_config = CanvasConfig(api_key=api_key, base_url=base_url)

        return cls(
            canvas_config=canvas_config,
            days_ahead=days_ahead,
            include_submitted=include_submitted,
        )

    async def fetch(self, since_cursor: Optional[str], account_id: str = "default") -> ConnectorFetchResult:
        del since_cursor  # Canvas API currently uses time windows, not cursor-based incremental sync.

        if not self.is_available:
            return ConnectorFetchResult(items=[], next_cursor=datetime.utcnow().isoformat())

        assert self._canvas_config is not None

        async with self._client_factory(self._canvas_config) as client:
            assignments = await client.get_upcoming_assignments(
                days=self._days_ahead,
                include_submitted=self._include_submitted,
            )
            events = await client.get_upcoming_events(days=self._days_ahead)

        items: list[ConnectorFetchItem] = []
        for assignment in assignments:
            items.extend(self._assignment_items(assignment))

        for event in events:
            event_item = self._event_item(event)
            if event_item is not None:
                items.append(event_item)

        return ConnectorFetchResult(items=items, next_cursor=datetime.utcnow().isoformat())

    def _assignment_items(self, assignment: CanvasAssignment) -> list[ConnectorFetchItem]:
        now = datetime.now(timezone.utc)
        due = assignment.due_at or (now + timedelta(hours=2))
        start = due - timedelta(hours=2)

        tags = ["canvas", "assignment"]
        if assignment.course_name:
            tags.append(assignment.course_name)
        if assignment.is_submitted:
            tags.append("submitted")

        task_item = ConnectorFetchItem(
            external_id=f"assignment-task-{assignment.id}",
            fingerprint=f"canvas:assignment:task:{assignment.id}:{due.isoformat()}",
            occurred_at=due,
            raw_payload=assignment.to_dict(),
            normalized_payload={
                "kind": "task",
                "title": f"[Canvas作业] {assignment.course_name}: {assignment.name}" if assignment.course_name else f"[Canvas作业] {assignment.name}",
                "description": assignment.description or assignment.html_url or "",
                "estimated_minutes": 120,
                "due_date": due.date().isoformat(),
                "priority": self._priority_from_days(assignment.days_left),
                "tags": tags,
                "origin_ref": f"canvas:assignment:{assignment.id}",
                "confidence": 1.0,
            },
        )

        deadline_item = ConnectorFetchItem(
            external_id=f"assignment-deadline-{assignment.id}",
            fingerprint=f"canvas:assignment:deadline:{assignment.id}:{due.isoformat()}",
            occurred_at=due,
            raw_payload=assignment.to_dict(),
            normalized_payload={
                "kind": "event",
                "title": f"[DDL] {assignment.name}",
                "start_time": start.isoformat(),
                "end_time": due.isoformat(),
                "description": assignment.html_url or assignment.description or "",
                "priority": self._priority_from_days(assignment.days_left),
                "tags": tags + ["deadline"],
                "origin_ref": f"canvas:assignment:{assignment.id}",
                "confidence": 1.0,
            },
        )

        return [task_item, deadline_item]

    def _event_item(self, event: CanvasEvent) -> Optional[ConnectorFetchItem]:
        start = event.start_at
        if start is None:
            return None

        end = event.end_at or (start + timedelta(hours=2))
        title = event.title or "Canvas 事件"

        return ConnectorFetchItem(
            external_id=f"event-{event.id}",
            fingerprint=f"canvas:event:{event.id}:{start.isoformat()}:{end.isoformat()}",
            occurred_at=start,
            raw_payload=event.to_dict(),
            normalized_payload={
                "kind": "event",
                "title": f"[Canvas] {title}",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
                "description": event.description or event.html_url or "",
                "priority": "medium",
                "tags": ["canvas", "event", event.course_name] if event.course_name else ["canvas", "event"],
                "origin_ref": f"canvas:event:{event.id}",
                "confidence": 1.0,
            },
        )

    @staticmethod
    def _priority_from_days(days_left: int) -> str:
        if days_left <= 1:
            return "urgent"
        if days_left <= 3:
            return "high"
        if days_left <= 7:
            return "medium"
        return "low"
