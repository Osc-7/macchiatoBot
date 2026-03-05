"""Canvas 工具测试。"""

import pytest

from schedule_agent.config import Config, LLMConfig, CanvasIntegrationConfig
from schedule_agent.core.tools.canvas_tools import SyncCanvasTool, FetchCanvasOverviewTool
from schedule_agent.storage.json_repository import EventRepository, TaskRepository


class _FakeCanvasConfig:
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url

    def validate(self) -> bool:
        return True


class _FakeCanvasClient:
    def __init__(self, config):
        self.config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None

    # For FetchCanvasOverviewTool
    async def get_user_profile(self):
        return {"id": 1, "name": "Test User", "login_id": "test@example.com"}

    async def get_courses(self):
        return [
            {"id": 1, "name": "SE101", "course_code": "SE101"},
        ]


class _FakeSyncResult:
    def __init__(self):
        self.created_count = 1
        self.skipped_count = 0
        self.updated_count = 0
        self.errors = []

    def to_dict(self):
        return {
            "created_count": self.created_count,
            "skipped_count": self.skipped_count,
            "updated_count": self.updated_count,
            "errors": self.errors,
        }


class _FakeCanvasSync:
    def __init__(self, client, event_creator=None):
        self.client = client
        self.event_creator = event_creator

    async def sync_to_schedule(self, days_ahead=60, include_submitted=False):
        if self.event_creator:
            await self.event_creator(
                {
                    "title": "[作业] CS101: HW1",
                    "start_time": "2026-03-01T10:00:00",
                    "end_time": "2026-03-01T12:00:00",
                    "description": "from canvas",
                    "priority": "high",
                    "tags": ["canvas", "作业"],
                    "metadata": {
                        "source": "canvas",
                        "canvas_id": 123,
                        "course_id": 456,
                        "type": "assignment",
                    },
                }
            )
        return _FakeSyncResult()


@pytest.mark.asyncio
async def test_sync_canvas_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("CANVAS_API_KEY", raising=False)
    config = Config(llm=LLMConfig(api_key="x", model="x"))
    tool = SyncCanvasTool(
        config=config,
        event_repository=EventRepository(tmp_path / "events.json"),
        task_repository=TaskRepository(tmp_path / "tasks.json"),
    )

    result = await tool.execute()
    assert result.success is False
    assert result.error == "CANVAS_DISABLED"


@pytest.mark.asyncio
async def test_sync_canvas_missing_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("CANVAS_API_KEY", raising=False)
    config = Config(
        llm=LLMConfig(api_key="x", model="x"),
        canvas=CanvasIntegrationConfig(enabled=True, api_key=None),
    )
    tool = SyncCanvasTool(
        config=config,
        event_repository=EventRepository(tmp_path / "events.json"),
        task_repository=TaskRepository(tmp_path / "tasks.json"),
    )

    result = await tool.execute()
    assert result.success is False
    assert result.error == "CANVAS_API_KEY_MISSING"


@pytest.mark.asyncio
async def test_sync_canvas_success_creates_task_and_deadline(monkeypatch, tmp_path):
    config = Config(
        llm=LLMConfig(api_key="x", model="x"),
        canvas=CanvasIntegrationConfig(
            enabled=True,
            api_key="dummy_canvas_key_12345",
            base_url="https://oc.sjtu.edu.cn/api/v1",
            default_days_ahead=30,
            include_submitted=False,
        ),
    )

    event_repo = EventRepository(tmp_path / "events.json")
    task_repo = TaskRepository(tmp_path / "tasks.json")
    tool = SyncCanvasTool(
        config=config,
        event_repository=event_repo,
        task_repository=task_repo,
    )

    monkeypatch.setattr("schedule_agent.core.tools.canvas_tools.CanvasConfig", _FakeCanvasConfig)
    monkeypatch.setattr("schedule_agent.core.tools.canvas_tools.CanvasClient", _FakeCanvasClient)
    monkeypatch.setattr("schedule_agent.core.tools.canvas_tools.CanvasSync", _FakeCanvasSync)

    result = await tool.execute(days_ahead=7, include_submitted=True)
    assert result.success is True
    assert result.data["created_count"] == 1
    assert result.metadata["source"] == "canvas"
    assert result.metadata["write_tasks"] is True
    assert result.metadata["write_deadline_events"] is True

    tasks = task_repo.get_all()
    assert len(tasks) == 1
    assert tasks[0].source == "canvas"
    assert tasks[0].origin_ref is not None
    assert tasks[0].deadline_event_id is not None

    events = event_repo.get_all()
    assert len(events) == 1
    assert events[0].event_type == "deadline"
    assert events[0].linked_task_id == tasks[0].id


@pytest.mark.asyncio
async def test_sync_canvas_submitted_marks_task_and_event_completed(monkeypatch, tmp_path):
    class _SubmittedCanvasSync(_FakeCanvasSync):
        async def sync_to_schedule(self, days_ahead=60, include_submitted=False):
            if self.event_creator:
                await self.event_creator(
                    {
                        "title": "[作业] CS101: HW1",
                        "start_time": "2026-03-01T10:00:00",
                        "end_time": "2026-03-01T12:00:00",
                        "description": "from canvas",
                        "priority": "high",
                        "tags": ["canvas", "作业", "已提交"],
                        "metadata": {
                            "source": "canvas",
                            "canvas_id": 999,
                            "course_id": 456,
                            "type": "assignment",
                        },
                    }
                )
            return _FakeSyncResult()

    config = Config(
        llm=LLMConfig(api_key="x", model="x"),
        canvas=CanvasIntegrationConfig(enabled=True, api_key="dummy_canvas_key_12345"),
    )

    event_repo = EventRepository(tmp_path / "events.json")
    task_repo = TaskRepository(tmp_path / "tasks.json")
    tool = SyncCanvasTool(
        config=config,
        event_repository=event_repo,
        task_repository=task_repo,
    )

    monkeypatch.setattr("schedule_agent.core.tools.canvas_tools.CanvasConfig", _FakeCanvasConfig)
    monkeypatch.setattr("schedule_agent.core.tools.canvas_tools.CanvasClient", _FakeCanvasClient)
    monkeypatch.setattr("schedule_agent.core.tools.canvas_tools.CanvasSync", _SubmittedCanvasSync)

    result = await tool.execute(include_submitted=True)
    assert result.success is True

    task = task_repo.get_all()[0]
    event = event_repo.get_all()[0]
    assert task.status.value == "completed"
    assert event.status.value == "completed"


@pytest.mark.asyncio
async def test_fetch_canvas_overview_success(monkeypatch, tmp_path):
    """FetchCanvasOverviewTool returns structured overview data."""

    class _FakeOverviewCanvasClient(_FakeCanvasClient):
        async def get_upcoming_assignments(self, days: int = 60, include_submitted: bool = False):
            from canvas_integration.models import CanvasAssignment

            return [
                CanvasAssignment(
                    id=201,
                    name="HW1",
                    course_id=1,
                    course_name="SE101",
                    points_possible=100.0,
                )
            ]

        async def get_upcoming_events(self, days: int = 60):
            from canvas_integration.models import CanvasEvent
            from datetime import datetime, timedelta, timezone

            start = datetime.now(timezone.utc)
            end = start + timedelta(hours=2)
            return [
                CanvasEvent(
                    id=301,
                    title="Lecture",
                    start_at=start,
                    end_at=end,
                    course_name="SE101",
                )
            ]

        async def get_planner_items(self, filter: str | None = None):
            from canvas_integration.models import CanvasPlannerItem

            return [
                CanvasPlannerItem(
                    plannable_id=401,
                    plannable_type="assignment",
                    title="HW1",
                    course_id=1,
                    course_name="SE101",
                )
            ]

    config = Config(
        llm=LLMConfig(api_key="x", model="x"),
        canvas=CanvasIntegrationConfig(
            enabled=True,
            api_key="dummy_canvas_key_12345",
        ),
    )

    tool = FetchCanvasOverviewTool(config=config)

    monkeypatch.setattr("schedule_agent.core.tools.canvas_tools.CanvasConfig", _FakeCanvasConfig)
    monkeypatch.setattr("schedule_agent.core.tools.canvas_tools.CanvasClient", _FakeOverviewCanvasClient)

    result = await tool.execute(days_ahead=7, include_submitted=True)
    assert result.success is True
    assert result.error is None
    assert "overview" in result.data
    overview = result.data["overview"]
    assert overview["profile"]["name"] == "Test User"
    assert len(overview["courses"]) == 1
    assert len(overview["upcoming_assignments"]) == 1
    assert len(overview["upcoming_events"]) == 1
    assert len(overview["planner_items"]) == 1
