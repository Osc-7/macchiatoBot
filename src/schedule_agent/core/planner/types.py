"""
Planner 领域类型定义。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class PlannerPlannedItem:
    """单条已规划结果。"""

    task_id: str
    start_at: datetime
    end_at: datetime
    score: float


@dataclass
class PlannerUnplannedItem:
    """单条未规划结果。"""

    task_id: str
    reason: str
    score: float


@dataclass
class PlannerResult:
    """规划结果聚合。"""

    planned_items: list[PlannerPlannedItem]
    unplanned_items: list[PlannerUnplannedItem]
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None

