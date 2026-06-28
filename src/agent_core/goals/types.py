"""Goal and step data models for agent session planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4


class GoalStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"


class GoalStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


@dataclass
class GoalStep:
    id: str
    description: str
    status: GoalStepStatus = GoalStepStatus.PENDING
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status.value,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> GoalStep:
        raw_status = str(data.get("status", GoalStepStatus.PENDING.value))
        try:
            status = GoalStepStatus(raw_status)
        except ValueError:
            status = GoalStepStatus.PENDING
        notes = data.get("notes")
        return cls(
            id=str(data.get("id", _new_id("step"))),
            description=str(data.get("description", "")).strip(),
            status=status,
            notes=str(notes).strip() if notes else None,
        )


@dataclass
class Goal:
    id: str
    title: str
    description: Optional[str] = None
    status: GoalStatus = GoalStatus.ACTIVE
    steps: List[GoalStep] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)

    def touch(self) -> None:
        self.updated_at = _utc_now_iso()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "steps": [step.to_dict() for step in self.steps],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Goal:
        raw_status = str(data.get("status", GoalStatus.ACTIVE.value))
        try:
            status = GoalStatus(raw_status)
        except ValueError:
            status = GoalStatus.ACTIVE
        steps_raw = data.get("steps") or []
        steps = [
            GoalStep.from_dict(item)
            for item in steps_raw
            if isinstance(item, dict)
        ]
        return cls(
            id=str(data.get("id", _new_id("goal"))),
            title=str(data.get("title", "")).strip(),
            description=(
                str(data.get("description")).strip()
                if data.get("description")
                else None
            ),
            status=status,
            steps=steps,
            created_at=str(data.get("created_at") or _utc_now_iso()),
            updated_at=str(data.get("updated_at") or _utc_now_iso()),
        )
