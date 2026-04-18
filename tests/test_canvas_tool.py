"""Canvas 工具测试。"""

import pytest

from agent_core.config import Config, LLMConfig, CanvasIntegrationConfig, FileToolsConfig
from system.tools.canvas_tools import (
    SyncCanvasTool,
    FetchCanvasOverviewTool,
    FetchCanvasCourseAssignmentsTool,
    FetchCanvasAssignmentDetailTool,
    FetchCanvasSubmissionTool,
    FetchCanvasAssignmentAttachmentsTool,
    DownloadCanvasFileTool,
)
from agent_core.storage.json_repository import EventRepository, TaskRepository


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

    monkeypatch.setattr("system.tools.canvas_tools.CanvasConfig", _FakeCanvasConfig)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasClient", _FakeCanvasClient)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasSync", _FakeCanvasSync)

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
async def test_sync_canvas_submitted_marks_task_and_event_completed(
    monkeypatch, tmp_path
):
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

    monkeypatch.setattr("system.tools.canvas_tools.CanvasConfig", _FakeCanvasConfig)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasClient", _FakeCanvasClient)
    monkeypatch.setattr(
        "system.tools.canvas_tools.CanvasSync", _SubmittedCanvasSync
    )

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
        async def get_upcoming_assignments(
            self, days: int = 60, include_submitted: bool = False
        ):
            from frontend.canvas_integration.models import CanvasAssignment

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
            from frontend.canvas_integration.models import CanvasEvent
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
            from frontend.canvas_integration.models import CanvasPlannerItem

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

    monkeypatch.setattr("system.tools.canvas_tools.CanvasConfig", _FakeCanvasConfig)
    monkeypatch.setattr(
        "system.tools.canvas_tools.CanvasClient", _FakeOverviewCanvasClient
    )

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


@pytest.mark.asyncio
async def test_fetch_canvas_course_assignments_success(monkeypatch, tmp_path):
    class _FakeAssignClient(_FakeCanvasClient):
        async def get_assignments(
            self, course_id, include_submission=True, all_dates=True, course_name=None
        ):
            from frontend.canvas_integration.models import CanvasAssignment

            return [
                CanvasAssignment(
                    id=501,
                    name="Lab 1",
                    course_id=course_id,
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
    tool = FetchCanvasCourseAssignmentsTool(config=config)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasConfig", _FakeCanvasConfig)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasClient", _FakeAssignClient)

    result = await tool.execute(course_id=1)
    assert result.success is True
    assert len(result.data["assignments"]) == 1
    assert result.data["assignments"][0]["id"] == 501


@pytest.mark.asyncio
async def test_fetch_canvas_assignment_detail_by_id(monkeypatch, tmp_path):
    class _FakeDetailClient(_FakeCanvasClient):
        async def get_assignment(
            self, course_id, assignment_id, include_submission=True, **kwargs
        ):
            from frontend.canvas_integration.models import CanvasAssignment

            return CanvasAssignment(
                id=assignment_id,
                name="Midterm",
                description="<p>Do problems 1-3</p>",
                course_id=course_id,
                course_name="SE101",
            )

    config = Config(
        llm=LLMConfig(api_key="x", model="x"),
        canvas=CanvasIntegrationConfig(enabled=True, api_key="dummy_canvas_key_12345"),
    )
    tool = FetchCanvasAssignmentDetailTool(config=config)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasConfig", _FakeCanvasConfig)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasClient", _FakeDetailClient)

    result = await tool.execute(course_id=1, assignment_id=77)
    assert result.success is True
    assert result.data["assignment"]["id"] == 77
    assert "problems" in result.data["assignment"]["description"]


@pytest.mark.asyncio
async def test_fetch_canvas_assignment_detail_ambiguous(monkeypatch, tmp_path):
    class _FakeAmbClient(_FakeCanvasClient):
        async def get_assignments(self, *args, **kwargs):
            from frontend.canvas_integration.models import CanvasAssignment

            return [
                CanvasAssignment(id=1, name="HW1 draft", course_id=1, course_name="X"),
                CanvasAssignment(id=2, name="HW1 final", course_id=1, course_name="X"),
            ]

    config = Config(
        llm=LLMConfig(api_key="x", model="x"),
        canvas=CanvasIntegrationConfig(enabled=True, api_key="dummy_canvas_key_12345"),
    )
    tool = FetchCanvasAssignmentDetailTool(config=config)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasConfig", _FakeCanvasConfig)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasClient", _FakeAmbClient)

    result = await tool.execute(course_id=1, assignment_search="HW1")
    assert result.success is False
    assert result.error == "ASSIGNMENT_AMBIGUOUS"


@pytest.mark.asyncio
async def test_fetch_canvas_submission_success(monkeypatch, tmp_path):
    class _FakeSubClient(_FakeCanvasClient):
        async def get_submission(self, course_id, assignment_id):
            return {
                "id": 900,
                "assignment_id": assignment_id,
                "workflow_state": "graded",
                "grade": "88",
                "body": "B" * 9000,
                "attachments": [{"id": 1, "display_name": "a.pdf", "url": "http://x"}],
            }

    config = Config(
        llm=LLMConfig(api_key="x", model="x"),
        canvas=CanvasIntegrationConfig(enabled=True, api_key="dummy_canvas_key_12345"),
    )
    tool = FetchCanvasSubmissionTool(config=config)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasConfig", _FakeCanvasConfig)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasClient", _FakeSubClient)

    result = await tool.execute(course_id=1, assignment_id=12, body_max_chars=4000)
    assert result.success is True
    assert result.data["submission"]["grade"] == "88"
    assert len(result.data["submission"]["body"]) == 4000
    assert result.data["submission"]["attachments"][0]["display_name"] == "a.pdf"


@pytest.mark.asyncio
async def test_fetch_canvas_assignment_attachments(monkeypatch, tmp_path):
    class _FakeAttClient(_FakeCanvasClient):
        async def fetch_assignment_dict(self, course_id, assignment_id, **kwargs):
            return {
                "id": assignment_id,
                "attachments": [
                    {
                        "id": 55,
                        "display_name": "hw.pdf",
                        "filename": "hw.pdf",
                        "content_type": "application/pdf",
                        "size": 1024,
                        "url": "http://example.invalid/download",
                    }
                ],
            }

    config = Config(
        llm=LLMConfig(api_key="x", model="x"),
        canvas=CanvasIntegrationConfig(enabled=True, api_key="dummy_canvas_key_12345"),
    )
    tool = FetchCanvasAssignmentAttachmentsTool(config=config)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasConfig", _FakeCanvasConfig)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasClient", _FakeAttClient)

    result = await tool.execute(course_id=1, assignment_id=10)
    assert result.success is True
    assert len(result.data["attachments"]) == 1
    assert result.data["attachments"][0]["id"] == 55
    assert "url" not in result.data["attachments"][0]


@pytest.mark.asyncio
async def test_download_canvas_file_writes_bytes(monkeypatch, tmp_path):
    class _FakeDlClient(_FakeCanvasClient):
        async def get_file_metadata(self, file_id, course_id=None):
            return {"id": file_id, "size": 5}

        async def download_file_bytes(self, file_id, course_id=None, assignment_id=None):
            return b"hello", "note.txt"

    out_path = tmp_path / "out.txt"

    def _fake_resolve(path_str, exec_ctx, ft_cfg, config):
        return out_path, None

    monkeypatch.setattr(
        "system.tools.canvas_tools._resolve_mutation_path_for_file_tool",
        _fake_resolve,
    )

    config = Config(
        llm=LLMConfig(api_key="x", model="x"),
        canvas=CanvasIntegrationConfig(enabled=True, api_key="dummy_canvas_key_12345"),
        file_tools=FileToolsConfig(allow_write=True),
    )
    tool = DownloadCanvasFileTool(config=config)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasConfig", _FakeCanvasConfig)
    monkeypatch.setattr("system.tools.canvas_tools.CanvasClient", _FakeDlClient)

    result = await tool.execute(
        file_id=9,
        dest_path="ignored/path.txt",
        course_id=1,
        __execution_context__={},
    )
    assert result.success is True
    assert out_path.read_bytes() == b"hello"
