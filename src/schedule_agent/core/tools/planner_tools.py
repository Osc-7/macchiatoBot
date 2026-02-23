"""
规划器工具 - 智能安排任务和时间规划

提供 get_free_slots（获取空闲时间段）和 plan_tasks（自动规划任务）工具。
"""

from datetime import datetime, date, timedelta
from typing import Optional, List, Tuple
from zoneinfo import ZoneInfo
import uuid

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from schedule_agent.storage.json_repository import EventRepository, TaskRepository
from schedule_agent.models import (
    Event, Task, TimeSlot, SlotType,
    EventStatus, TaskStatus, TaskPriority
)


def _to_naive_local(dt: datetime, tz: str = "Asia/Shanghai") -> datetime:
    """将 datetime 转为 naive 本地时间，统一 planner 内的时间比较"""
    if dt.tzinfo is not None:
        return dt.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
    return dt


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
        """
        初始化获取空闲时间段工具。

        Args:
            event_repository: 事件存储仓库（可选，默认创建新实例）
            sleep_start_hour: 睡眠开始小时（默认 23 点）
            sleep_start_minute: 睡眠开始分钟（默认 0）
            sleep_end_hour: 睡眠结束小时（默认 8 点）
            sleep_end_minute: 睡眠结束分钟（默认 0）
        """
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
            tags=['日程', '规划', '查询'],
        )

    def _create_sleep_slots(self, target_date: date) -> List[TimeSlot]:
        """
        为指定日期创建睡眠时间段。

        包括：前一天晚上到当天早上的睡眠时间（如果查询包含当天早上）
        和当天晚上的睡眠时间。

        Args:
            target_date: 目标日期

        Returns:
            睡眠时间段列表
        """
        sleep_slots = []

        # 前一天晚上到当天早上的睡眠时间
        prev_day = target_date - timedelta(days=1)
        sleep_start = datetime(
            prev_day.year, prev_day.month, prev_day.day,
            self._sleep_start_hour, self._sleep_start_minute
        )
        wake_time = datetime(
            target_date.year, target_date.month, target_date.day,
            self._sleep_end_hour, self._sleep_end_minute
        )
        sleep_slots.append(TimeSlot(
            start_time=sleep_start,
            end_time=wake_time,
            slot_type=SlotType.SLEEP,
            title="睡眠时间"
        ))

        # 当天晚上的睡眠时间
        evening_sleep_start = datetime(
            target_date.year, target_date.month, target_date.day,
            self._sleep_start_hour, self._sleep_start_minute
        )
        next_day = target_date + timedelta(days=1)
        next_wake_time = datetime(
            next_day.year, next_day.month, next_day.day,
            self._sleep_end_hour, self._sleep_end_minute
        )
        sleep_slots.append(TimeSlot(
            start_time=evening_sleep_start,
            end_time=next_wake_time,
            slot_type=SlotType.SLEEP,
            title="睡眠时间"
        ))

        return sleep_slots

    def _get_busy_slots(
        self,
        start_date: date,
        end_date: date,
    ) -> List[TimeSlot]:
        """
        获取指定日期范围内的忙碌时间段（已有事件）。

        Args:
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            忙碌时间段列表
        """
        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date, datetime.max.time())

        events = self._event_repository.get_by_date_range(start_dt, end_dt)

        busy_slots = []
        for event in events:
            if event.status != EventStatus.CANCELLED:
                busy_slots.append(TimeSlot(
                    start_time=_to_naive_local(event.start_time),
                    end_time=_to_naive_local(event.end_time),
                    slot_type=SlotType.BUSY,
                    title=event.title,
                    metadata={"event_id": event.id}
                ))

        return busy_slots

    def _calculate_free_slots(
        self,
        start_dt: datetime,
        end_dt: datetime,
        busy_slots: List[TimeSlot],
        sleep_slots: List[TimeSlot],
    ) -> List[TimeSlot]:
        """
        计算空闲时间段。

        Args:
            start_dt: 开始时间
            end_dt: 结束时间
            busy_slots: 忙碌时间段列表
            sleep_slots: 睡眠时间段列表

        Returns:
            空闲时间段列表
        """
        # 合并所有占用的时间段
        occupied_slots = busy_slots + sleep_slots

        # 按开始时间排序
        occupied_slots.sort(key=lambda s: s.start_time)

        # 计算空闲时间段
        free_slots = []
        current_start = start_dt

        for slot in occupied_slots:
            # 只处理与查询范围有交集的时间段
            if slot.end_time <= current_start:
                continue
            if slot.start_time >= end_dt:
                break

            # 如果当前开始时间到这个占用时间段开始之间有空隙
            if current_start < slot.start_time:
                free_end = min(slot.start_time, end_dt)
                if free_end > current_start:
                    free_slots.append(TimeSlot(
                        start_time=current_start,
                        end_time=free_end,
                        slot_type=SlotType.FREE,
                    ))

            # 更新当前开始时间为这个占用时间段的结束时间
            current_start = max(current_start, slot.end_time)

        # 处理最后一个占用时间段之后的空闲时间
        if current_start < end_dt:
            free_slots.append(TimeSlot(
                start_time=current_start,
                end_time=end_dt,
                slot_type=SlotType.FREE,
            ))

        return free_slots

    async def execute(self, **kwargs) -> ToolResult:
        """
        执行获取空闲时间段操作。

        Args:
            date: 查询日期（可选，默认今天）
            days: 查询天数（可选，默认1）
            min_duration: 最小时长过滤（可选）

        Returns:
            操作结果，包含空闲时间段列表
        """
        # 解析日期
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

        # 解析天数
        days = kwargs.get("days", 1)
        if not isinstance(days, int) or days < 1:
            days = 1

        # 解析最小时长过滤
        min_duration = kwargs.get("min_duration")

        # 计算日期范围
        start_date = target_date
        end_date = target_date + timedelta(days=days - 1)

        # 时间范围
        start_dt = datetime.combine(start_date, datetime.min.time())
        # 结束时间设置为最后一天的 23:00（避免和下一天的睡眠时间重叠）
        end_dt = datetime.combine(end_date, datetime.min.time()) + timedelta(hours=23)

        # 获取忙碌时间段
        busy_slots = self._get_busy_slots(start_date, end_date)

        # 获取睡眠时间段
        sleep_slots = []
        for d in range(days):
            current_date = start_date + timedelta(days=d)
            sleep_slots.extend(self._create_sleep_slots(current_date))

        # 计算空闲时间段
        free_slots = self._calculate_free_slots(start_dt, end_dt, busy_slots, sleep_slots)

        # 过滤最小时长
        if min_duration is not None:
            if not isinstance(min_duration, int) or min_duration < 1:
                min_duration = None
            else:
                free_slots = [s for s in free_slots if s.duration_minutes >= min_duration]

        # 计算总空闲时长
        total_free_minutes = sum(s.duration_minutes for s in free_slots)

        # 构建结果消息
        if days == 1:
            date_desc = target_date.strftime("%Y-%m-%d (%A)")
        else:
            date_desc = f"{start_date} 到 {end_date}"

        message = f"{date_desc} 共有 {len(free_slots)} 个空闲时间段，总计 {total_free_minutes} 分钟（约 {total_free_minutes // 60} 小时）"

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
    """
    自动规划任务工具

    将待办任务自动安排到空闲时间段中。
    """

    def __init__(
        self,
        event_repository: Optional[EventRepository] = None,
        task_repository: Optional[TaskRepository] = None,
        sleep_start_hour: int = 23,
        sleep_start_minute: int = 0,
        sleep_end_hour: int = 8,
        sleep_end_minute: int = 0,
    ):
        """
        初始化自动规划任务工具。

        Args:
            event_repository: 事件存储仓库（可选）
            task_repository: 任务存储仓库（可选）
            sleep_start_hour: 睡眠开始小时
            sleep_start_minute: 睡眠开始分钟
            sleep_end_hour: 睡眠结束小时
            sleep_end_minute: 睡眠结束分钟
        """
        self._event_repository = event_repository or EventRepository()
        self._task_repository = task_repository or TaskRepository()
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
            description="""自动将待办任务安排到空闲时间段。

这是智能规划的核心工具，用于:
- 自动安排待办任务
- 按优先级和截止日期排序
- 考虑任务的预计时长
- 避免与现有日程冲突

工具会自动:
- 获取待办任务列表
- 按优先级（紧急>高>中>低）和截止日期排序
- 查找合适的空闲时间段
- 为任务安排时间（创建事件）
- 更新任务状态为"已安排"

注意：规划结果会实际创建日程事件，请确认后再执行。""",
            parameters=[
                ToolParameter(
                    name="days",
                    type="integer",
                    description="规划未来多少天（默认3天）",
                    required=False,
                    default=3,
                ),
                ToolParameter(
                    name="max_tasks",
                    type="integer",
                    description="最多规划多少个任务（默认5个，防止一次创建太多日程）",
                    required=False,
                    default=5,
                ),
                ToolParameter(
                    name="task_ids",
                    type="array",
                    description="指定要规划的任务ID列表（可选，如果不指定则自动选择待办任务）",
                    required=False,
                ),
                ToolParameter(
                    name="prefer_morning",
                    type="boolean",
                    description="是否优先安排在上午（默认true）",
                    required=False,
                    default=True,
                ),
            ],
            examples=[
                {
                    "description": "自动规划未来3天的任务",
                    "params": {"days": 3, "max_tasks": 5},
                },
                {
                    "description": "规划最多10个任务",
                    "params": {"days": 7, "max_tasks": 10},
                },
                {
                    "description": "指定要规划的任务",
                    "params": {
                        "task_ids": ["task-001", "task-002"],
                        "days": 3,
                    },
                },
            ],
            usage_notes=[
                "任务会按优先级和截止日期自动排序",
                "如果空闲时间不足，会返回未安排的任务列表",
                "规划后任务状态会变为 in_progress",
                "每个任务会创建对应的日程事件",
            ],
            tags=['任务', '规划'],
        )

    def _get_free_slots_for_planning(
        self,
        start_date: date,
        days: int,
        prefer_morning: bool,
    ) -> List[TimeSlot]:
        """
        获取用于规划的空闲时间段。

        Args:
            start_date: 开始日期
            days: 天数
            prefer_morning: 是否优先上午

        Returns:
            空闲时间段列表
        """
        get_free_slots_tool = GetFreeSlotsTool(
            event_repository=self._event_repository,
            sleep_start_hour=self._sleep_start_hour,
            sleep_start_minute=self._sleep_start_minute,
            sleep_end_hour=self._sleep_end_hour,
            sleep_end_minute=self._sleep_end_minute,
        )

        # 计算日期范围
        end_date = start_date + timedelta(days=days - 1)

        # 时间范围
        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date, datetime.min.time()) + timedelta(hours=23)

        # 获取忙碌时间段
        busy_slots = []
        current = start_date
        for _ in range(days):
            events = self._event_repository.get_by_date(current)
            for event in events:
                if event.status != EventStatus.CANCELLED:
                    busy_slots.append(TimeSlot(
                        start_time=_to_naive_local(event.start_time),
                        end_time=_to_naive_local(event.end_time),
                        slot_type=SlotType.BUSY,
                        title=event.title,
                    ))
            current += timedelta(days=1)

        # 获取睡眠时间段
        sleep_slots = []
        for d in range(days):
            current_date = start_date + timedelta(days=d)
            prev_day = current_date - timedelta(days=1)
            sleep_start = datetime(
                prev_day.year, prev_day.month, prev_day.day,
                self._sleep_start_hour, self._sleep_start_minute
            )
            wake_time = datetime(
                current_date.year, current_date.month, current_date.day,
                self._sleep_end_hour, self._sleep_end_minute
            )
            sleep_slots.append(TimeSlot(
                start_time=sleep_start,
                end_time=wake_time,
                slot_type=SlotType.SLEEP,
            ))
            evening_sleep_start = datetime(
                current_date.year, current_date.month, current_date.day,
                self._sleep_start_hour, self._sleep_start_minute
            )
            next_day = current_date + timedelta(days=1)
            next_wake_time = datetime(
                next_day.year, next_day.month, next_day.day,
                self._sleep_end_hour, self._sleep_end_minute
            )
            sleep_slots.append(TimeSlot(
                start_time=evening_sleep_start,
                end_time=next_wake_time,
                slot_type=SlotType.SLEEP,
            ))

        # 计算空闲时间段
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
                    free_slots.append(TimeSlot(
                        start_time=current_start,
                        end_time=free_end,
                        slot_type=SlotType.FREE,
                    ))

            current_start = max(current_start, slot.end_time)

        if current_start < end_dt:
            free_slots.append(TimeSlot(
                start_time=current_start,
                end_time=end_dt,
                slot_type=SlotType.FREE,
            ))

        # 如果优先上午，按开始时间排序；否则也按开始时间排序
        free_slots.sort(key=lambda s: s.start_time)

        # 如果优先上午，把上午的时间段放前面
        if prefer_morning:
            morning_slots = []
            afternoon_slots = []
            for slot in free_slots:
                if slot.start_time.hour < 12:
                    morning_slots.append(slot)
                else:
                    afternoon_slots.append(slot)
            free_slots = morning_slots + afternoon_slots

        return free_slots

    def _get_tasks_to_plan(
        self,
        task_ids: Optional[List[str]],
        max_tasks: int,
    ) -> List[Task]:
        """
        获取要规划的任务列表。

        Args:
            task_ids: 指定的任务 ID 列表
            max_tasks: 最大任务数量

        Returns:
            要规划的任务列表
        """
        if task_ids:
            # 使用指定的任务
            tasks = []
            for tid in task_ids:
                task = self._task_repository.get(tid)
                if task and task.status == TaskStatus.TODO:
                    tasks.append(task)
        else:
            # 获取所有待办任务
            tasks = self._task_repository.get_todo()

        # 按优先级和截止日期排序
        priority_order = {
            TaskPriority.URGENT: 0,
            TaskPriority.HIGH: 1,
            TaskPriority.MEDIUM: 2,
            TaskPriority.LOW: 3,
        }
        tasks.sort(key=lambda t: (
            priority_order.get(t.priority, 2),
            t.due_date or date.max,
        ))

        # 限制数量
        return tasks[:max_tasks]

    def _find_suitable_slot(
        self,
        task: Task,
        free_slots: List[TimeSlot],
    ) -> Optional[TimeSlot]:
        """
        为任务找到合适的空闲时间段。

        Args:
            task: 要安排的任务
            free_slots: 空闲时间段列表

        Returns:
            合适的时间段，如果找不到则返回 None
        """
        required_minutes = task.estimated_minutes or 60

        for slot in free_slots:
            if slot.can_fit(required_minutes):
                return slot

        return None

    async def execute(self, **kwargs) -> ToolResult:
        """
        执行自动规划任务操作。

        Args:
            days: 规划天数
            max_tasks: 最大任务数量
            task_ids: 指定任务 ID 列表
            prefer_morning: 是否优先上午

        Returns:
            操作结果，包含规划结果
        """
        # 解析参数
        days = kwargs.get("days", 3)
        if not isinstance(days, int) or days < 1:
            days = 3

        max_tasks = kwargs.get("max_tasks", 5)
        if not isinstance(max_tasks, int) or max_tasks < 1:
            max_tasks = 5

        task_ids = kwargs.get("task_ids")
        prefer_morning = kwargs.get("prefer_morning", True)

        # 获取要规划的任务
        tasks_to_plan = self._get_tasks_to_plan(task_ids, max_tasks)

        if not tasks_to_plan:
            return ToolResult(
                success=True,
                data={
                    "planned_tasks": [],
                    "unplanned_tasks": [],
                    "created_events": [],
                },
                message="没有待办任务需要规划",
            )

        # 获取空闲时间段
        start_date = date.today()
        free_slots = self._get_free_slots_for_planning(start_date, days, prefer_morning)

        if not free_slots:
            return ToolResult(
                success=False,
                error="NO_FREE_SLOTS",
                message="没有找到空闲时间段，无法规划任务",
            )

        # 规划任务
        planned_tasks = []
        unplanned_tasks = []
        created_events = []

        # 用于跟踪已占用的时间段
        occupied_during_planning = []

        for task in tasks_to_plan:
            required_minutes = task.estimated_minutes or 60
            found_slot = None

            # 在空闲时间段中找到合适的
            for slot in free_slots:
                # 检查这个时间段是否已经被规划占用
                is_occupied = False
                for occupied in occupied_during_planning:
                    if slot.overlaps_with(occupied):
                        is_occupied = True
                        break

                if not is_occupied and slot.can_fit(required_minutes):
                    found_slot = slot
                    break

            if found_slot:
                # 创建日程事件
                task_end = found_slot.start_time + timedelta(minutes=required_minutes)

                event = Event(
                    title=f"[任务] {task.title}",
                    description=f"自动规划的任务\n任务ID: {task.id}\n{task.description or ''}",
                    start_time=found_slot.start_time,
                    end_time=task_end,
                    priority=task.priority,
                    tags=task.tags + ["自动规划"],
                )

                # 保存事件
                created_event = self._event_repository.create(event)

                # 更新任务状态
                task.schedule(found_slot.start_time, task_end)
                task.status = TaskStatus.IN_PROGRESS
                self._task_repository.update(task)

                # 记录已占用的时间段
                occupied_during_planning.append(TimeSlot(
                    start_time=found_slot.start_time,
                    end_time=task_end,
                    slot_type=SlotType.BUSY,
                ))

                planned_tasks.append({
                    "task_id": task.id,
                    "task_title": task.title,
                    "scheduled_start": found_slot.start_time.isoformat(),
                    "scheduled_end": task_end.isoformat(),
                    "event_id": created_event.id,
                })
                created_events.append(created_event)
            else:
                unplanned_tasks.append({
                    "task_id": task.id,
                    "task_title": task.title,
                    "estimated_minutes": required_minutes,
                    "reason": "没有足够长的空闲时间段",
                })

        # 构建结果消息
        message_parts = []
        if planned_tasks:
            message_parts.append(f"成功规划 {len(planned_tasks)} 个任务")
        if unplanned_tasks:
            message_parts.append(f"{len(unplanned_tasks)} 个任务因时间不足未能安排")

        message = "，".join(message_parts) if message_parts else "没有任务需要规划"

        return ToolResult(
            success=True,
            data={
                "planned_tasks": planned_tasks,
                "unplanned_tasks": unplanned_tasks,
                "created_events": created_events,
                "summary": {
                    "total_tasks": len(tasks_to_plan),
                    "planned": len(planned_tasks),
                    "unplanned": len(unplanned_tasks),
                    "planning_days": days,
                },
            },
            message=message,
            metadata={
                "days": days,
                "max_tasks": max_tasks,
                "prefer_morning": prefer_morning,
            },
        )
