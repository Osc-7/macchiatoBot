"""
Agent 目标追踪工具 — 会话内多步骤工作的结构化计划。

参考 Mastra TaskSignalProvider（task_write / task_update / task_complete / task_check）
与 OpenClaw proactive-tasks 技能的设计：Agent 在复杂任务中维护可持久化的目标与步骤清单，
并在 system prompt 中自动注入当前进度。
"""

from __future__ import annotations

from typing import List, Optional

from agent_core.goals.store import GoalStore
from agent_core.goals.types import GoalStepStatus
from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult


def build_goal_tools(store: GoalStore) -> List[BaseTool]:
    """构建绑定到同一 GoalStore 的目标工具集。"""
    return [
        GoalCreateTool(store),
        GoalUpdateTool(store),
        GoalCompleteTool(store),
        GoalListTool(store),
    ]


def _goal_summary(store: GoalStore, goal_id: str) -> dict:
    goal = store.get_goal(goal_id)
    if goal is None:
        return {}
    return goal.to_dict()


class GoalCreateTool(BaseTool):
    """创建新的工作目标及可选步骤。"""

    def __init__(self, store: GoalStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "goal_create"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""创建新的 Agent 工作目标（会话内多步骤计划）。

当用户提出复杂、多步骤任务时使用，例如：
- 调研并撰写报告
- 重构模块并跑测试
- 分阶段完成部署/调试

与用户日程中的「待办任务」(add_task) 不同：目标是 Agent 当前会话的工作计划，
用于跟踪自己接下来要做什么，并在 system prompt 中保持可见。

创建后请用 goal_update 标记 in_progress 的步骤，完成后用 goal_complete。""",
            parameters=[
                ToolParameter(
                    name="title",
                    type="string",
                    description="目标标题，简洁描述要完成的事",
                    required=True,
                ),
                ToolParameter(
                    name="description",
                    type="string",
                    description="可选补充说明",
                    required=False,
                ),
                ToolParameter(
                    name="steps",
                    type="array",
                    description="可选初始步骤列表（字符串数组）",
                    required=False,
                    items={"type": "string"},
                ),
            ],
            examples=[
                {
                    "description": "创建调研报告目标",
                    "params": {
                        "title": "撰写竞品分析报告",
                        "steps": [
                            "搜索并收集资料",
                            "整理要点",
                            "撰写初稿",
                            "校对并输出",
                        ],
                    },
                },
            ],
            usage_notes=[
                "复杂任务开始时先 goal_create，再逐步推进",
                "与用户待办 add_task 无关，勿混淆",
            ],
            tags=["目标", "计划", "多步骤"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        title = str(kwargs.get("title", "")).strip()
        if not title:
            return ToolResult(
                success=False,
                error="MISSING_TITLE",
                message="请提供目标标题",
            )
        description = kwargs.get("description")
        steps_raw = kwargs.get("steps")
        steps: Optional[List[str]] = None
        if isinstance(steps_raw, list):
            steps = [str(s) for s in steps_raw]
        try:
            goal = self._store.create_goal(
                title=title,
                description=str(description).strip() if description else None,
                steps=steps,
            )
        except ValueError as exc:
            return ToolResult(success=False, error="INVALID_ARGUMENTS", message=str(exc))
        return ToolResult(
            success=True,
            data={"goal": goal.to_dict()},
            message=f"已创建目标 {goal.id}: {goal.title}",
        )


class GoalUpdateTool(BaseTool):
    """更新目标或步骤状态。"""

    def __init__(self, store: GoalStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "goal_update"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""更新 Agent 目标的标题、步骤或步骤状态。

用于：
- 将某步骤标为 in_progress（开始执行）
- 标记 blocked 并写明原因
- 追加新步骤
- 修正目标标题/描述""",
            parameters=[
                ToolParameter(
                    name="goal_id",
                    type="string",
                    description="目标 ID（goal_create 返回）",
                    required=True,
                ),
                ToolParameter(
                    name="step_id",
                    type="string",
                    description="要更新的步骤 ID（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="status",
                    type="string",
                    description="步骤状态：pending | in_progress | blocked",
                    required=False,
                    enum=["pending", "in_progress", "blocked"],
                ),
                ToolParameter(
                    name="notes",
                    type="string",
                    description="步骤备注（如 blocked 原因）",
                    required=False,
                ),
                ToolParameter(
                    name="title",
                    type="string",
                    description="更新目标标题",
                    required=False,
                ),
                ToolParameter(
                    name="description",
                    type="string",
                    description="更新目标描述",
                    required=False,
                ),
                ToolParameter(
                    name="add_steps",
                    type="array",
                    description="追加的新步骤（字符串数组）",
                    required=False,
                    items={"type": "string"},
                ),
            ],
            examples=[
                {
                    "description": "开始执行某步骤",
                    "params": {
                        "goal_id": "goal-abc12345",
                        "step_id": "step-def67890",
                        "status": "in_progress",
                    },
                },
                {
                    "description": "标记阻塞",
                    "params": {
                        "goal_id": "goal-abc12345",
                        "step_id": "step-def67890",
                        "status": "blocked",
                        "notes": "等待用户提供 API key",
                    },
                },
            ],
            tags=["目标", "计划", "更新"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        goal_id = str(kwargs.get("goal_id", "")).strip()
        if not goal_id:
            return ToolResult(
                success=False,
                error="MISSING_GOAL_ID",
                message="请提供 goal_id",
            )
        step_id = kwargs.get("step_id")
        step_id_str = str(step_id).strip() if step_id else None
        raw_status = kwargs.get("status")
        step_status: Optional[GoalStepStatus] = None
        if raw_status:
            try:
                step_status = GoalStepStatus(str(raw_status))
            except ValueError:
                return ToolResult(
                    success=False,
                    error="INVALID_STATUS",
                    message=f"无效状态: {raw_status}",
                )
        add_steps_raw = kwargs.get("add_steps")
        add_steps: Optional[List[str]] = None
        if isinstance(add_steps_raw, list):
            add_steps = [str(s) for s in add_steps_raw]
        try:
            goal = self._store.update_goal(
                goal_id,
                title=kwargs.get("title"),
                description=kwargs.get("description"),
                step_id=step_id_str,
                step_status=step_status,
                step_notes=kwargs.get("notes"),
                add_steps=add_steps,
            )
        except KeyError as exc:
            return ToolResult(success=False, error="NOT_FOUND", message=str(exc))
        except ValueError as exc:
            return ToolResult(success=False, error="INVALID_ARGUMENTS", message=str(exc))
        return ToolResult(
            success=True,
            data={"goal": goal.to_dict()},
            message=f"已更新目标 {goal.id}",
        )


class GoalCompleteTool(BaseTool):
    """完成步骤或整个目标。"""

    def __init__(self, store: GoalStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "goal_complete"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""将 Agent 目标的某步骤或整个目标标记为 completed。

- 提供 step_id：仅完成该步骤；若所有步骤均完成则自动完成目标
- 不提供 step_id：完成目标及全部未完成步骤""",
            parameters=[
                ToolParameter(
                    name="goal_id",
                    type="string",
                    description="目标 ID",
                    required=True,
                ),
                ToolParameter(
                    name="step_id",
                    type="string",
                    description="要完成的步骤 ID（可选；省略则完成整个目标）",
                    required=False,
                ),
                ToolParameter(
                    name="notes",
                    type="string",
                    description="完成备注",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "完成单个步骤",
                    "params": {
                        "goal_id": "goal-abc12345",
                        "step_id": "step-def67890",
                        "notes": "已收集 5 篇参考",
                    },
                },
            ],
            tags=["目标", "计划", "完成"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        goal_id = str(kwargs.get("goal_id", "")).strip()
        if not goal_id:
            return ToolResult(
                success=False,
                error="MISSING_GOAL_ID",
                message="请提供 goal_id",
            )
        step_id = kwargs.get("step_id")
        step_id_str = str(step_id).strip() if step_id else None
        try:
            goal = self._store.complete(
                goal_id,
                step_id=step_id_str,
                notes=kwargs.get("notes"),
            )
        except KeyError as exc:
            return ToolResult(success=False, error="NOT_FOUND", message=str(exc))
        if goal.status.value == "completed":
            msg = f"目标 {goal.id} 已全部完成"
        elif step_id_str:
            msg = f"步骤 {step_id_str} 已完成"
        else:
            msg = f"目标 {goal.id} 已标记完成"
        return ToolResult(
            success=True,
            data={"goal": goal.to_dict()},
            message=msg,
        )


class GoalListTool(BaseTool):
    """列出当前会话的目标。"""

    def __init__(self, store: GoalStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "goal_list"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""列出当前会话的 Agent 工作目标及步骤进度。

活跃目标也会自动注入 system prompt；需要详细 JSON 或查看已完成目标时调用本工具。""",
            parameters=[
                ToolParameter(
                    name="include_completed",
                    type="boolean",
                    description="是否包含已完成/已取消的目标，默认 false",
                    required=False,
                    default=False,
                ),
            ],
            tags=["目标", "计划", "查询"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        include_completed = bool(kwargs.get("include_completed", False))
        goals = self._store.list_goals(include_completed=include_completed)
        data = {"goals": [g.to_dict() for g in goals], "count": len(goals)}
        if not goals:
            return ToolResult(
                success=True,
                data=data,
                message="当前没有活跃目标",
            )
        return ToolResult(
            success=True,
            data=data,
            message=f"共 {len(goals)} 个目标",
        )
