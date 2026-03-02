"""Tests for SJTU undergraduate timetable fetch tool."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, List

import pytest

from schedule_agent.config import SjtuJwConfig
from schedule_agent.core.tools.sjtu_jw_tools import (
    FetchSjtuUndergradScheduleTool,
    SjtuJwClientError,
    guess_academic_year_and_term,
)


def test_guess_academic_year_and_term_rules():
    """Verify academic year/term guessing logic around year boundaries."""
    # 2025-10 -> 2025-2026 第一学期
    year, term = guess_academic_year_and_term(date(2025, 10, 1))
    assert year == "2025"
    assert term == "1"

    # 2026-01 -> 2025-2026 第一学期
    year, term = guess_academic_year_and_term(date(2026, 1, 15))
    assert year == "2025"
    assert term == "1"

    # 2026-03 -> 2025-2026 第二学期
    year, term = guess_academic_year_and_term(date(2026, 3, 1))
    assert year == "2025"
    assert term == "2"


class _FakeClient:
    def __init__(self, cookies_path: Path):
        self.cookies_path = cookies_path
        self.calls: List[Dict[str, Any]] = []

    async def fetch_courses(self, year: str, term: str) -> List[Dict[str, Any]]:
        self.calls.append({"year": year, "term": term})
        return [
            {
                "kch_id": "CS101",
                "jxbmc": "CS101-1",
                "kcmc": "计算机导论",
                "cdmc": "东上院101",
                "xqj": 1,
                "jcs": "1-2",
                "xm": "张老师",
                "zcd": "1-16周",
                "xkbz": "",
            }
        ]


@pytest.mark.asyncio
async def test_fetch_tool_success_uses_client_and_normalizes():
    """Tool should call client and normalize course items."""
    fake_client = _FakeClient(Path("dummy.json"))

    def factory(path: Path) -> _FakeClient:  # type: ignore[override]
        assert path == Path("dummy.json")
        return fake_client

    tool = FetchSjtuUndergradScheduleTool(
        cookies_path="dummy.json",
        client_factory=factory,
        config=SjtuJwConfig(),
    )

    result = await tool.execute(year="2025", term="1")

    assert result.success is True
    assert result.error is None
    assert result.data is not None
    assert result.data["year"] == "2025"
    assert result.data["term"] == "1"
    assert result.data["course_count"] == 1
    courses = result.data["courses"]
    assert isinstance(courses, list)
    assert courses[0]["course_id"] == "CS101"
    assert courses[0]["start_section"] == 1
    assert courses[0]["section_count"] == 2

    # client should be invoked once with provided year/term
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["year"] == "2025"
    assert fake_client.calls[0]["term"] == "1"


class _ErrorClient:
    def __init__(self, cookies_path: Path):
        self.cookies_path = cookies_path

    async def fetch_courses(self, year: str, term: str) -> List[Dict[str, Any]]:
        raise SjtuJwClientError("NOT_LOGGED_IN")


@pytest.mark.asyncio
async def test_fetch_tool_handles_not_logged_in_error():
    """Tool should map NOT_LOGGED_IN client error to friendly ToolResult."""
    def factory(path: Path) -> _ErrorClient:  # type: ignore[override]
        return _ErrorClient(path)

    tool = FetchSjtuUndergradScheduleTool(
        cookies_path="dummy.json",
        client_factory=factory,
        config=SjtuJwConfig(),
    )

    result = await tool.execute(year="2025", term="1")

    assert result.success is False
    assert result.error == "NOT_LOGGED_IN"
    assert "登录" in result.message


class _FlakyClient:
    """First call fails with NOT_LOGGED_IN, second call succeeds."""

    def __init__(self, cookies_path: Path):
        self.cookies_path = cookies_path
        self.calls: List[Dict[str, Any]] = []
        self._attempts = 0

    async def fetch_courses(self, year: str, term: str) -> List[Dict[str, Any]]:
        self._attempts += 1
        self.calls.append({"year": year, "term": term, "attempt": self._attempts})
        if self._attempts == 1:
            raise SjtuJwClientError("NOT_LOGGED_IN")
        return [
            {
                "kch_id": "CS102",
                "jxbmc": "CS102-1",
                "kcmc": "数据结构",
                "cdmc": "电院201",
                "xqj": 2,
                "jcs": "3-4",
                "xm": "李老师",
                "zcd": "1-16周",
                "xkbz": "",
            }
        ]


@pytest.mark.asyncio
async def test_fetch_tool_refresh_cookies_triggers_login_and_retry():
    """When refresh_cookies=true and first call is NOT_LOGGED_IN, tool should invoke login_helper then retry."""

    flaky_client = _FlakyClient(Path("dummy.json"))

    def factory(path: Path) -> _FlakyClient:  # type: ignore[override]
        assert path == Path("dummy.json")
        return flaky_client

    login_calls: list[Path] = []

    async def login_helper(path: Path) -> None:
        login_calls.append(path)

    tool = FetchSjtuUndergradScheduleTool(
        cookies_path="dummy.json",
        client_factory=factory,
        config=SjtuJwConfig(),
        login_helper=login_helper,
    )

    result = await tool.execute(year="2025", term="1", refresh_cookies=True)

    assert result.success is True
    assert result.error is None
    assert result.data["course_count"] == 1
    assert login_calls == [Path("dummy.json")]

