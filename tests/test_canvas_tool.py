"""Canvas 工具测试。"""

import pytest

from schedule_agent.config import Config, LLMConfig, CanvasIntegrationConfig
from schedule_agent.core.tools.canvas_tools import SyncCanvasTool
from schedule_agent.core.tools.base import ToolResult


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
        self.called = False

    async def sync_to_schedule(self, days_ahead=60, include_submitted=False):
        self.called = True
        if self.event_creator:
            await self.event_creator(
                {
                    "title": "Canvas Demo",
                    "start_time": "2026-03-01T10:00:00",
                    "end_time": "2026-03-01T11:00:00",
                    "description": "from canvas",
                    "priority": "medium",
                    "tags": ["canvas"],
                }
            )
        return _FakeSyncResult()


@pytest.mark.asyncio
async def test_sync_canvas_missing_api_key(monkeypatch):
    monkeypatch.delenv("CANVAS_API_KEY", raising=False)
    config = Config(llm=LLMConfig(api_key="x", model="x"))
    tool = SyncCanvasTool(config=config)

    result = await tool.execute()
    assert result.success is False
    assert result.error == "CANVAS_API_KEY_MISSING"


@pytest.mark.asyncio
async def test_sync_canvas_success(monkeypatch):
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
    tool = SyncCanvasTool(config=config)

    async def _fake_create_event(**kwargs):
        return ToolResult(
            success=True,
            message="ok",
            metadata={"event_id": "evt_001"},
        )

    monkeypatch.setattr("schedule_agent.core.tools.canvas_tools.CanvasConfig", _FakeCanvasConfig)
    monkeypatch.setattr("schedule_agent.core.tools.canvas_tools.CanvasClient", _FakeCanvasClient)
    monkeypatch.setattr("schedule_agent.core.tools.canvas_tools.CanvasSync", _FakeCanvasSync)
    monkeypatch.setattr(tool._add_event_tool, "execute", _fake_create_event)

    result = await tool.execute(days_ahead=7, include_submitted=True)
    assert result.success is True
    assert result.data["created_count"] == 1
    assert result.metadata["source"] == "canvas"
