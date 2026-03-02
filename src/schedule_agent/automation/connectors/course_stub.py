"""Stub course connector."""

from __future__ import annotations

from datetime import datetime, timedelta

from .base import BaseConnector, ConnectorFetchItem, ConnectorFetchResult


class CourseConnectorStub(BaseConnector):
    source_type = "course"

    async def fetch(self, since_cursor: str | None, account_id: str = "default") -> ConnectorFetchResult:
        now = datetime.utcnow()
        external_id = f"course-{now.strftime('%Y%m%d')}"
        item = ConnectorFetchItem(
            external_id=external_id,
            fingerprint=f"{external_id}:{account_id}",
            occurred_at=now,
            raw_payload={"source": "course_stub", "account_id": account_id},
            normalized_payload={
                "kind": "event",
                "title": "[课表] 自动同步课程",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=1)).isoformat(),
                "description": "由自动化课表同步生成",
                "tags": ["course", "auto-sync"],
                "confidence": 1.0,
            },
        )
        return ConnectorFetchResult(items=[item], next_cursor=now.isoformat())
