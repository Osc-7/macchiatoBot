"""Stub email connector."""

from __future__ import annotations

from datetime import datetime, timedelta

from .base import BaseConnector, ConnectorFetchItem, ConnectorFetchResult


class EmailConnectorStub(BaseConnector):
    source_type = "email"

    async def fetch(self, since_cursor: str | None, account_id: str = "default") -> ConnectorFetchResult:
        now = datetime.utcnow()
        external_id = f"email-{now.strftime('%Y%m%d')}"
        item = ConnectorFetchItem(
            external_id=external_id,
            fingerprint=f"{external_id}:{account_id}",
            occurred_at=now,
            raw_payload={"source": "email_stub", "account_id": account_id},
            normalized_payload={
                "kind": "task",
                "title": "[邮件] 跟进自动同步通知",
                "description": "由自动化邮件同步生成",
                "estimated_minutes": 30,
                "due_date": (now + timedelta(days=1)).date().isoformat(),
                "tags": ["email", "auto-sync", "needs_review"],
                "confidence": 0.75,
            },
        )
        return ConnectorFetchResult(items=[item], next_cursor=now.isoformat())
