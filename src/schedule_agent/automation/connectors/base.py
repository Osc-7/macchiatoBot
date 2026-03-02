"""Connector abstraction for external sync sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class ConnectorFetchItem:
    external_id: str
    fingerprint: str
    occurred_at: datetime
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    normalized_payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectorFetchResult:
    items: List[ConnectorFetchItem] = field(default_factory=list)
    next_cursor: Optional[str] = None


class BaseConnector:
    source_type: str = "base"

    async def fetch(self, since_cursor: Optional[str], account_id: str = "default") -> ConnectorFetchResult:
        raise NotImplementedError
