"""Lightweight async event bus."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List


EventHandler = Callable[[dict], Awaitable[None]]


@dataclass
class BusEvent:
    topic: str
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


class AsyncEventBus:
    def __init__(self):
        self._handlers: Dict[str, List[EventHandler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: EventHandler) -> None:
        self._handlers[topic].append(handler)

    async def publish(self, topic: str, payload: Dict[str, Any]) -> int:
        handlers = list(self._handlers.get(topic, []))
        if not handlers:
            return 0

        event = BusEvent(topic=topic, payload=payload)
        results = await asyncio.gather(
            *[handler({"topic": event.topic, "payload": event.payload, "created_at": event.created_at.isoformat()}) for handler in handlers],
            return_exceptions=True,
        )

        failures = [r for r in results if isinstance(r, Exception)]
        if failures:
            raise RuntimeError(f"event handlers failed for topic '{topic}': {len(failures)}")
        return len(handlers)
