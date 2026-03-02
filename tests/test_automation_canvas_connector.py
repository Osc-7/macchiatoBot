"""Tests for Canvas automation connector."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from canvas_integration.config import CanvasConfig
from canvas_integration.models import CanvasAssignment, CanvasEvent
from schedule_agent.automation.connectors.canvas import CanvasConnector


class _FakeCanvasClient:
    def __init__(self, config: CanvasConfig):
        self._config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_upcoming_assignments(self, days: int, include_submitted: bool = False):
        due = datetime.now(timezone.utc) + timedelta(days=2)
        return [
            CanvasAssignment(
                id=101,
                name="Lab 1",
                description="Finish lab",
                course_id=1,
                course_name="SE101",
                due_at=due,
                is_submitted=False,
                html_url="https://canvas.example/a/101",
            )
        ]

    async def get_upcoming_events(self, days: int):
        start = datetime.now(timezone.utc) + timedelta(days=1)
        end = start + timedelta(hours=2)
        return [
            CanvasEvent(
                id=202,
                title="Lecture",
                description="Week 3 lecture",
                start_at=start,
                end_at=end,
                course_name="SE101",
            )
        ]


@pytest.mark.asyncio
async def test_canvas_connector_fetch_generates_task_and_events():
    connector = CanvasConnector(
        canvas_config=CanvasConfig(api_key="x" * 24, base_url="https://canvas.example/api/v1"),
        client_factory=_FakeCanvasClient,
    )

    result = await connector.fetch(since_cursor=None)

    assert len(result.items) == 3
    kinds = [item.normalized_payload["kind"] for item in result.items]
    assert kinds.count("task") == 1
    assert kinds.count("event") == 2


@pytest.mark.asyncio
async def test_canvas_connector_unavailable_returns_empty():
    connector = CanvasConnector(canvas_config=None, client_factory=_FakeCanvasClient)
    result = await connector.fetch(since_cursor=None)

    assert result.items == []
    assert isinstance(result.next_cursor, str)
