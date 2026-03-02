"""
规划器工具 - 智能安排任务和时间规划

提供 get_free_slots（获取空闲时间段）和 plan_tasks（自动规划任务）工具。
"""

from datetime import datetime, date, timedelta
from typing import Optional, List
import uuid

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from schedule_agent.config import PlanningConfig
from schedule_agent.storage.json_repository import EventRepository, TaskRepository
from schedule_agent.models import (
    Event,
    Task,
    TimeSlot,
    SlotType,
    EventStatus,
    TaskStatus,
)
from schedule_agent.core.planner import PlannerEngine


class GetFreeSlotsTool(BaseTool):
    """
    获取空闲时间段工具

    分析指定日期范围内的空闲时间，考虑已有事件和睡眠时间。
    """

    def __init__(
        self,
        event_repository: Optional[EventRepository] = None,
        sleep_start_hour: int = 23,
        sleep_start_minute: int = 0,
        sleep_end_hour: int = 8,
        sleep_end_minute: int = 0,
    ):
        self._event_repository = event_repository or EventRepository()
        self._sleep_start_hour = sleep_start_hour
        self._sleep_start_minute = sleep_start_minute
        self._sleep_end_hour = sleep_end_hour
        self._sleep_end_minute = sleep_end_minute

    @property
    def name(self) -> str:
        return "get_free_slots"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_free_slots",
            description="""获取指定日期范围内的空闲时间段。

这是规划时间的关键工具，用于:
- 查看某天/某周有哪些空闲时间
- 了解什么时间段可以安排新任务
- 分析日程的忙碌程度

工具会自动:
- 排除已有事件占用的时段
- 排除睡眠时间（默认 23:00-08:00）
- 合并相邻的空闲时间段
- 计算总空闲时长

返回结果按开始时间排序。""",
            parameters=[
                ToolParameter(
                    name="date",
                    type="string",
                    description="查询日期，格式: YYYY-MM-DD（可选，默认今天）",
                    required=False,
                ),
                ToolParameter(
                    name="days",
                    type="integer",
                    description="查询多少天（可选，默认1天）",
                    required=False,
                    default=1,
                ),
                ToolParameter(
                    name="min_duration",
                    type="integer",
                    description="最小时长过滤（分钟），只返回时长不小于此值的时间段（可选）",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "查看今天的空闲时间",
                    "params": {"date": "2026-02-17"},
                },
                {
                    "description": "查看未来3天的空闲时间",
                    "params": {"date": "2026-02-17", "days": 3},
                },
                {
                    "description": "查看今天至少1小时的空闲时间段",
                    "params": {"date": "2026-02-17", "min_duration": 60},
                },
            ],
            usage_notes=[
                "默认查询今天",
                "睡眠时间（默认23:00-08:00）会被自动排除",
                "结果按开始时间排序",
                "可以使用 min_duration 过滤掉太短的时间段",
            ],
            tags=["日程", "规划", "查询"],
        )

    def _create_sleep_slots(self, target_date: date) -> List[TimeSlot]:
        sleep_slots = []

        prev_day = target_date - timedelta(days=1)
        sleep_start = datetime(
            prev_day.year,
            prev_day.month,
            prev_day.day,
            self._sleep_start_hour,
            self._sleep_start_minute,
        )
        wake_time = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            self._sleep_end_hour,
            self._sleep_end_minute,
        )
        sleep_slots.append(
            TimeSlot(
                start_time=sleep_start,
                end_time=wake_time,
                slot_type=SlotType.SLEEP,
                title="睡眠时间",
            )
        )

        evening_sleep_start = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            self._sleep_start_hour,
            self._sleep_start_minute,
        )
        next_day = target_date + timedelta(days=1)
        next_wake_time = datetime(
            next_day.year,
            next_day.month,
            next_day.day,
            self._sleep_end_hour,
            self._sleep_end_minute,
        )
        sleep_slots.append(
            TimeSlot(
                start_time=evening_sleep_start,
                end_time=next_wake_time,
                slot_type=SlotType.SLEEP,
                title="睡眠时间",
            )
        )

        return sleep_slots

    def _get_busy_slots(self, start_date: date, end_date: date) -> List[TimeSlot]:
        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date, datetime.max.time())

        events = self._event_repository.get_by_date_range(start_dt, end_dt)

        busy_slots = []
        for event in events:
            if event.status != EventStatus.CANCELLED:
                busy_slots.append(
                    TimeSlot(
                        start_time=event.start_time,
                        end_time=event.end_time,
                        slot_type=SlotType.BUSY,
                        title=event.title,
                        metadata={"event_id": event.id},
                    )
                )

        return busy_slots

    def _calculate_free_slots(
        self,
        start_dt: datetime,
        end_dt: datetime,
        busy_slots: List[TimeSlot],
        sleep_slots: List[TimeSlot],
    ) -> List[TimeSlot]:
        occupied_slots = busy_slots + sleep_slots
        occupied_slots.sort(key=lambda s: s.start_time)

        free_slots = []
        current_start = start_dt

        for slot in occupied_slots:
            if slot.end_time <= current_start:
                continue
            if slot.start_time >= end_dt:
                break

            if current_start < slot.start_time:
                free_end = min(slot.start_time, end_dt)
                if free_end > current_start:
                    free_slots.append(
                        TimeSlot(
                            start_time=current_start,
                            end_time=free_end,
                            slot_type=SlotType.FREE,
                        )
                    )

            current_start = max(current_start, slot.end_time)

        if current_start < end_dt:
            free_slots.append(
                TimeSlot(
                    start_time=current_start,
                    end_time=end_dt,
                    slot_type=SlotType.FREE,
                )
            )

        return free_slots

    async def execute(self, **kwargs) -> ToolResult:
        date_str = kwargs.get("date")
        if date_str:
            try:
                target_date = date.fromisoformat(date_str)
            except ValueError as e:
                return ToolResult(
                    success=False,
                    error="INVALID_DATE_FORMAT",
                    message=f"日期格式无效: {str(e)}，请使用 YYYY-MM-DD 格式",
                )
        else:
            target_date = date.today()

        days = kwargs.get("days", 1)
        if not isinstance(days, int) or days < 1:
            days = 1

        min_duration = kwargs.get("min_duration")

        start_date = target_date
        end_date = target_date + timedelta(days=days - 1)

        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date, datetime.min.time()) + timedelta(hours=23)

        busy_slots = self._get_busy_slots(start_date, end_date)

        sleep_slots = []
        for d in range(days):
            current_date = start_date + timedelta(days=d)
            sleep_slots.extend(self._create_sleep_slots(current_date))

        free_slots = self._calculate_free_slots(start_dt, end_dt, busy_slots, sleep_slots)

        if min_duration is not None:
            if not isinstance(min_duration, int) or min_duration < 1:
                min_duration = None
            else:
                free_slots = [s for s in free_slots if s.duration_minutes >= min_duration]

        total_free_minutes = sum(s.duration_minutes for s in free_slots)

        if days == 1:
            date_desc = target_date.strftime("%Y-%m-%d (%A)")
        else:
            date_desc = f"{start_date} 到 {end_date}"

        message = (
            f"{date_desc} 共有 {len(free_slots)} 个空闲时间段，总计 {total_free_minutes} 分钟"
            f"（约 {total_free_minutes // 60} 小时）"
        )

        return ToolResult(
            success=True,
            data={
                "free_slots": free_slots,
                "total_count": len(free_slots),
                "total_minutes": total_free_minutes,
                "query_range": {
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "days": days,
                },
            },
            message=message,
            metadata={
                "query_date": target_date.isoformat(),
                "days": days,
                "min_duration": min_duration,
            },
        )


class PlanTasksTool(BaseTool):
    """自动规划任务工具。"""

    def __init__(
        self,
        event_repository: Optional[EventRepository] = None,
        task_repository: Optional[TaskRepository] = None,
        planning_config: Optional[PlanningConfig] = None,
        sleep_start_hour: int = 23,
        sleep_start_minute: int = 0,
        sleep_end_hour: int = 8,
        sleep_end_minute: int = 0,
    ):
        self._event_repository = event_repository or EventRepository()
        self._task_repository = task_repository or TaskRepository()
        self._planning_config = planning_config or PlanningConfig()
        # 兼容旧构造参数（一期不使用睡眠窗口作为主规划约束）
        self._sleep_start_hour = sleep_start_hour
        self._sleep_start_minute = sleep_start_minute
        self._sleep_end_hour = sleep_end_hour
        self._sleep_end_minute = sleep_end_minute

    @property
    def name(self) -> str:
        return "plan_tasks"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="plan_tasks",
            description="""在用户工作时段内自动安排任务。

Planner 会：
- 按 ddl 紧迫度 + 难度 + 用户重视度打分
- 避开已有 blocking 事件
- 生成 planned_block 并写回日程

该工具是稳定入口，后续算法升级不影响调用方式。""",
            parameters=[
                ToolParameter(
                    name="start_date",
                    type="string",
                    description="规划起始日期（YYYY-MM-DD，默认今天）",
                    required=False,
                ),
                ToolParameter(
                    name="days",
                    type="integer",
                    description="规划天数（默认使用配置 planning.lookahead_days）",
                    required=False,
                ),
                ToolParameter(
                    name="max_tasks",
                    type="integer",
                    description="最多规划多少个任务（默认50）",
                    required=False,
                    default=50,
                ),
                ToolParameter(
                    name="task_ids",
                    type="array",
                    description="指定任务 ID 列表（可选）",
                    required=False,
                ),
                ToolParameter(
                    name="prefer_morning",
                    type="boolean",
                    description="兼容参数：一期保留但当前算法不使用（可选）",
                    required=False,
                    default=True,
                ),
                ToolParameter(
                    name="replace_existing_plans",
                    type="boolean",
                    description="是否先覆盖旧的 planned_block（默认 true）",
                    required=False,
                    default=True,
                ),
                ToolParameter(
                    name="dry_run",
                    type="boolean",
                    description="是否仅返回规划结果但不落库（默认 false）",
                    required=False,
                    default=False,
                ),
            ],
            examples=[
                {
                    "description": "规划未来 7 天任务",
                    "params": {"days": 7},
                },
                {
                    "description": "只规划指定任务并覆盖旧计划",
                    "params": {
                        "task_ids": ["task-001", "task-002"],
                        "replace_existing_plans": True,
                    },
                },
                {
                    "description": "先 dry run 查看排程",
                    "params": {"days": 3, "dry_run": True},
                },
            ],
            usage_notes=[
                "需要在 config.planning.working_hours 中配置工作时段",
                "replace_existing_plans=true 时会取消同窗口内旧计划块",
                "planner 生成的事件类型为 planned_block，来源 source=planner",
            ],
            tags=["任务", "规划"],
        )

    def _get_tasks_to_plan(self, task_ids: Optional[List[str]], max_tasks: int) -> List[Task]:
        if task_ids:
            tasks = []
            for tid in task_ids:
                task = self._task_repository.get(tid)
                if task and task.status == TaskStatus.TODO:
                    tasks.append(task)
        else:
            tasks = self._task_repository.get_todo()

        if max_tasks < 1:
            max_tasks = 1
        return tasks[:max_tasks]

    def _cancel_existing_planned_blocks(self, start_dt: datetime, end_dt: datetime) -> int:
        events = self._event_repository.get_by_date_range(start_dt, end_dt)
        cancelled = 0
        for event in events:
            if (
                event.source == "planner"
                and event.event_type == "planned_block"
                and event.status != EventStatus.CANCELLED
            ):
                event.status = EventStatus.CANCELLED
                event.update_timestamp()
                self._event_repository.update(event)
                cancelled += 1
        return cancelled

    async def execute(self, **kwargs) -> ToolResult:
        if not self._planning_config.working_hours:
            return ToolResult(
                success=False,
                error="PLANNING_CONFIG_MISSING",
                message="未配置 planning.working_hours，无法进行任务规划",
            )

        start_date_str = kwargs.get("start_date")
        if start_date_str:
            try:
                start_date = date.fromisoformat(start_date_str)
            except ValueError:
                return ToolResult(
                    success=False,
                    error="INVALID_START_DATE",
                    message="start_date 格式无效，请使用 YYYY-MM-DD",
                )
        else:
            start_date = date.today()

        days = kwargs.get("days", self._planning_config.lookahead_days)
        if not isinstance(days, int) or days < 1:
            days = self._planning_config.lookahead_days

        max_tasks = kwargs.get("max_tasks", 50)
        if not isinstance(max_tasks, int) or max_tasks < 1:
            max_tasks = 50

        task_ids = kwargs.get("task_ids")
        _ = kwargs.get("prefer_morning", True)
        replace_existing_plans = kwargs.get("replace_existing_plans", True)
        dry_run = kwargs.get("dry_run", False)

        tasks_to_plan = self._get_tasks_to_plan(task_ids, max_tasks)
        if not tasks_to_plan:
            return ToolResult(
                success=True,
                data={
                    "plan_run_id": str(uuid.uuid4()),
                    "planned_tasks": [],
                    "unplanned_tasks": [],
                    "created_events": [],
                    "planned_items": [],
                    "unplanned_items": [],
                    "summary": {
                        "total": 0,
                        "planned": 0,
                        "unplanned": 0,
                        "window_start": None,
                        "window_end": None,
                        "dry_run": dry_run,
                    },
                },
                message="没有待办任务需要规划",
            )

        query_start = datetime.combine(start_date, datetime.min.time())
        query_end_date = start_date + timedelta(days=days)
        query_end = datetime.combine(query_end_date, datetime.min.time())

        events = self._event_repository.get_by_date_range(query_start, query_end)

        if replace_existing_plans:
            # 在重算时忽略旧 planner 计划块，避免历史计划阻塞新计划。
            events = [
                event
                for event in events
                if not (event.source == "planner" and event.event_type == "planned_block")
            ]

        engine = PlannerEngine(self._planning_config)
        plan_result = engine.plan(
            tasks=tasks_to_plan,
            events=events,
            start_date=start_date,
            days=days,
        )

        plan_run_id = str(uuid.uuid4())

        cancelled_count = 0
        if replace_existing_plans and not dry_run:
            cancelled_count = self._cancel_existing_planned_blocks(query_start, query_end)

        planned_items = []
        planned_tasks = []
        created_events = []
        for item in plan_result.planned_items:
            task = self._task_repository.get(item.task_id)
            if task is None:
                continue

            created_event_id = None
            if not dry_run:
                event = Event(
                    title=f"[任务] {task.title}",
                    description=f"自动规划任务\n任务ID: {task.id}\n{task.description or ''}",
                    start_time=item.start_at,
                    end_time=item.end_at,
                    priority=task.priority,
                    tags=task.tags + ["自动规划"],
                    source="planner",
                    event_type="planned_block",
                    is_blocking=True,
                    linked_task_id=task.id,
                    plan_run_id=plan_run_id,
                    metadata={
                        "score": round(item.score, 6),
                    },
                )
                created = self._event_repository.create(event)
                created_event_id = created.id
                created_events.append(created)

                task.schedule(item.start_at, item.end_at)
                self._task_repository.update(task)

            planned_items.append(
                {
                    "task_id": task.id,
                    "task_title": task.title,
                    "start_at": item.start_at.isoformat(),
                    "end_at": item.end_at.isoformat(),
                    "score": round(item.score, 6),
                    "event_id": created_event_id,
                }
            )
            planned_tasks.append(
                {
                    "task_id": task.id,
                    "task_title": task.title,
                    "scheduled_start": item.start_at.isoformat(),
                    "scheduled_end": item.end_at.isoformat(),
                    "event_id": created_event_id,
                    "score": round(item.score, 6),
                }
            )

        unplanned_items = [
            {
                "task_id": item.task_id,
                "reason": item.reason,
                "score": round(item.score, 6),
            }
            for item in plan_result.unplanned_items
        ]
        unplanned_tasks = []
        for item in unplanned_items:
            task = self._task_repository.get(item["task_id"])
            unplanned_tasks.append(
                {
                    "task_id": item["task_id"],
                    "task_title": task.title if task else item["task_id"],
                    "reason": item["reason"],
                    "score": item["score"],
                }
            )

        summary = {
            "total": len(tasks_to_plan),
            "planned": len(planned_items),
            "unplanned": len(unplanned_items),
            "window_start": plan_result.window_start.isoformat() if plan_result.window_start else None,
            "window_end": plan_result.window_end.isoformat() if plan_result.window_end else None,
            "dry_run": dry_run,
            "cancelled_previous_plans": cancelled_count,
        }

        message_parts = [f"成功规划 {len(planned_items)} 个任务"]
        if unplanned_items:
            message_parts.append(f"{len(unplanned_items)} 个任务未安排")
        if cancelled_count:
            message_parts.append(f"已取消 {cancelled_count} 个旧计划块")

        return ToolResult(
            success=True,
            data={
                "plan_run_id": plan_run_id,
                "planned_tasks": planned_tasks,
                "unplanned_tasks": unplanned_tasks,
                "created_events": created_events,
                "planned_items": planned_items,
                "unplanned_items": unplanned_items,
                "summary": summary,
            },
            message="，".join(message_parts),
            metadata={
                "start_date": start_date.isoformat(),
                "days": days,
                "replace_existing_plans": bool(replace_existing_plans),
                "dry_run": bool(dry_run),
            },
        )
