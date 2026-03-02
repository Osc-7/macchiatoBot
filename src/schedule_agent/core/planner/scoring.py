"""
Planner 评分逻辑。
"""

from datetime import date
from typing import Iterable

from schedule_agent.config import PlanningWeightsConfig
from schedule_agent.models import Task


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def score_task(task: Task, today: date, weights: PlanningWeightsConfig) -> float:
    """
    计算任务优先分数。

    一期按 DDL 紧迫度、难度、重视度和逾期加权计算。
    """
    if task.due_date is None:
        days_to_due = 14
    else:
        days_to_due = (task.due_date - today).days

    urgency_score = _clamp(1 - (days_to_due / 14), 0.0, 1.0)
    difficulty_score = _clamp(task.difficulty / 5, 0.0, 1.0)
    importance_score = _clamp(task.importance / 5, 0.0, 1.0)
    overdue_bonus = 1.0 if days_to_due < 0 else 0.0

    return (
        weights.urgency * urgency_score
        + weights.difficulty * difficulty_score
        + weights.importance * importance_score
        + weights.overdue_bonus * overdue_bonus
    )


def rank_tasks(
    tasks: Iterable[Task],
    today: date,
    weights: PlanningWeightsConfig,
) -> list[tuple[Task, float]]:
    """对任务打分并排序。"""
    scored = [(task, score_task(task, today, weights)) for task in tasks]
    scored.sort(
        key=lambda item: (
            -item[1],
            item[0].due_date or date.max,
            -item[0].importance,
            -item[0].difficulty,
        )
    )
    return scored

