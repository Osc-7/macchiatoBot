"""In-memory goal store with checkpoint serialization."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .types import Goal, GoalStatus, GoalStep, GoalStepStatus, _new_id


class GoalStore:
    """Session-scoped goal tracker for multi-step agent work."""

    def __init__(self) -> None:
        self._goals: Dict[str, Goal] = {}

    def list_goals(self, *, include_completed: bool = False) -> List[Goal]:
        goals = list(self._goals.values())
        if include_completed:
            return sorted(goals, key=lambda g: g.updated_at, reverse=True)
        return sorted(
            [g for g in goals if g.status == GoalStatus.ACTIVE],
            key=lambda g: g.updated_at,
            reverse=True,
        )

    def get_goal(self, goal_id: str) -> Optional[Goal]:
        return self._goals.get(goal_id)

    def create_goal(
        self,
        *,
        title: str,
        description: Optional[str] = None,
        steps: Optional[List[str]] = None,
    ) -> Goal:
        title = title.strip()
        if not title:
            raise ValueError("title 不能为空")
        goal = Goal(id=_new_id("goal"), title=title, description=description)
        for step_text in steps or []:
            text = str(step_text).strip()
            if text:
                goal.steps.append(
                    GoalStep(id=_new_id("step"), description=text)
                )
        self._goals[goal.id] = goal
        return goal

    def update_goal(
        self,
        goal_id: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        step_id: Optional[str] = None,
        step_status: Optional[GoalStepStatus] = None,
        step_notes: Optional[str] = None,
        add_steps: Optional[List[str]] = None,
    ) -> Goal:
        goal = self._require_goal(goal_id)
        if title is not None:
            title = title.strip()
            if not title:
                raise ValueError("title 不能为空")
            goal.title = title
        if description is not None:
            goal.description = description.strip() or None
        if add_steps:
            for step_text in add_steps:
                text = str(step_text).strip()
                if text:
                    goal.steps.append(
                        GoalStep(id=_new_id("step"), description=text)
                    )
        if step_id:
            step = self._require_step(goal, step_id)
            if step_status is not None:
                step.status = step_status
            if step_notes is not None:
                step.notes = step_notes.strip() or None
        goal.touch()
        return goal

    def complete(
        self,
        goal_id: str,
        *,
        step_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Goal:
        goal = self._require_goal(goal_id)
        if step_id:
            step = self._require_step(goal, step_id)
            step.status = GoalStepStatus.COMPLETED
            if notes:
                step.notes = notes.strip()
        else:
            for step in goal.steps:
                if step.status != GoalStepStatus.COMPLETED:
                    step.status = GoalStepStatus.COMPLETED
            goal.status = GoalStatus.COMPLETED
        goal.touch()
        if goal.steps and all(
            s.status == GoalStepStatus.COMPLETED for s in goal.steps
        ):
            goal.status = GoalStatus.COMPLETED
        return goal

    def cancel_goal(self, goal_id: str) -> Goal:
        goal = self._require_goal(goal_id)
        goal.status = GoalStatus.CANCELLED
        goal.touch()
        return goal

    def to_prompt_string(self) -> str:
        active = self.list_goals(include_completed=False)
        if not active:
            return ""
        lines: List[str] = []
        for goal in active:
            lines.append(f"## 目标 {goal.id}: {goal.title}")
            if goal.description:
                lines.append(goal.description)
            if goal.steps:
                for step in goal.steps:
                    mark = {
                        GoalStepStatus.PENDING: "[ ]",
                        GoalStepStatus.IN_PROGRESS: "[→]",
                        GoalStepStatus.BLOCKED: "[!]",
                        GoalStepStatus.COMPLETED: "[x]",
                    }.get(step.status, "[ ]")
                    suffix = f" — {step.notes}" if step.notes else ""
                    lines.append(
                        f"- {mark} {step.id}: {step.description}{suffix}"
                    )
            else:
                lines.append("- （尚无步骤，可用 goal_update 添加）")
            lines.append("")
        return "\n".join(lines).rstrip()

    def to_checkpoint_data(self) -> List[Dict[str, Any]]:
        return [goal.to_dict() for goal in self._goals.values()]

    def load_from_checkpoint(self, data: Optional[List[Dict[str, Any]]]) -> None:
        self._goals.clear()
        if not data:
            return
        for item in data:
            if isinstance(item, dict):
                goal = Goal.from_dict(item)
                self._goals[goal.id] = goal

    def _require_goal(self, goal_id: str) -> Goal:
        goal = self._goals.get(goal_id)
        if goal is None:
            raise KeyError(f"目标 '{goal_id}' 不存在")
        return goal

    def _require_step(self, goal: Goal, step_id: str) -> GoalStep:
        for step in goal.steps:
            if step.id == step_id:
                return step
        raise KeyError(f"步骤 '{step_id}' 不存在于目标 '{goal.id}'")
