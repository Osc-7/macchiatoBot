"""
数据模型测试

测试 Event, Task, TimeSlot 模型的核心功能
"""

import pytest
from datetime import datetime, timedelta, date

from schedule_agent.models import (
    Event, EventStatus, EventPriority,
    Task, TaskStatus, TaskPriority,
    TimeSlot, SlotType, create_sleep_slots
)


class TestEvent:
    """Event 模型测试"""

    def test_create_event_with_required_fields(self):
        """测试创建事件（必填字段）"""
        now = datetime.now()
        event = Event(
            title="团队会议",
            start_time=now,
            end_time=now + timedelta(hours=1)
        )

        assert event.title == "团队会议"
        assert event.id is not None
        assert event.status == EventStatus.SCHEDULED
        assert event.priority == EventPriority.MEDIUM
        assert len(event.tags) == 0

    def test_create_event_with_all_fields(self):
        """测试创建事件（所有字段）"""
        start = datetime(2026, 2, 17, 14, 0)
        event = Event(
            title="项目评审",
            description="Q1 项目进度评审",
            start_time=start,
            end_time=start + timedelta(hours=2),
            location="会议室 A",
            status=EventStatus.SCHEDULED,
            priority=EventPriority.HIGH,
            tags=["项目", "评审"],
            reminders=[15, 30]
        )

        assert event.title == "项目评审"
        assert event.description == "Q1 项目进度评审"
        assert event.location == "会议室 A"
        assert event.priority == EventPriority.HIGH
        assert event.tags == ["项目", "评审"]
        assert event.reminders == [15, 30]

    def test_event_duration_minutes(self):
        """测试事件时长计算"""
        start = datetime(2026, 2, 17, 9, 0)
        event = Event(
            title="晨会",
            start_time=start,
            end_time=start + timedelta(minutes=30)
        )

        assert event.duration_minutes == 30

    def test_event_is_all_day(self):
        """测试全天事件判断"""
        start = datetime(2026, 2, 17, 0, 0)

        # 短事件
        short_event = Event(
            title="短会议",
            start_time=start,
            end_time=start + timedelta(hours=1)
        )
        assert not short_event.is_all_day

        # 全天事件
        all_day_event = Event(
            title="全天活动",
            start_time=start,
            end_time=start + timedelta(hours=24)
        )
        assert all_day_event.is_all_day

    def test_event_conflict_detection(self):
        """测试事件冲突检测"""
        base = datetime(2026, 2, 17, 10, 0)

        event1 = Event(
            title="会议1",
            start_time=base,
            end_time=base + timedelta(hours=1)
        )

        # 重叠事件
        event2 = Event(
            title="会议2",
            start_time=base + timedelta(minutes=30),
            end_time=base + timedelta(hours=1, minutes=30)
        )
        assert event1.is_conflict_with(event2)

        # 不重叠事件（紧邻）
        event3 = Event(
            title="会议3",
            start_time=base + timedelta(hours=1),
            end_time=base + timedelta(hours=2)
        )
        assert not event1.is_conflict_with(event3)

        # 完全分离的事件
        event4 = Event(
            title="会议4",
            start_time=base + timedelta(hours=2),
            end_time=base + timedelta(hours=3)
        )
        assert not event1.is_conflict_with(event4)

    def test_cancelled_event_no_conflict(self):
        """测试已取消的事件不算冲突"""
        base = datetime(2026, 2, 17, 10, 0)

        event1 = Event(
            title="会议1",
            start_time=base,
            end_time=base + timedelta(hours=1)
        )

        event2 = Event(
            title="会议2",
            start_time=base,
            end_time=base + timedelta(hours=1),
            status=EventStatus.CANCELLED
        )

        assert not event1.is_conflict_with(event2)

    def test_event_str_representation(self):
        """测试事件字符串表示"""
        start = datetime(2026, 2, 17, 14, 0)
        event = Event(
            id="abc12345",
            title="团队周会",
            start_time=start,
            end_time=start + timedelta(hours=1)
        )

        result = str(event)
        assert "abc12345" in result
        assert "团队周会" in result
        assert "2026-02-17 14:00" in result


class TestTask:
    """Task 模型测试"""

    def test_create_task_with_required_fields(self):
        """测试创建任务（必填字段）"""
        task = Task(title="完成报告")

        assert task.title == "完成报告"
        assert task.id is not None
        assert task.status == TaskStatus.TODO
        assert task.priority == TaskPriority.MEDIUM
        assert task.estimated_minutes == 60  # 默认值

    def test_create_task_with_all_fields(self):
        """测试创建任务（所有字段）"""
        task = Task(
            title="编写测试用例",
            description="为数据模型编写完整的测试",
            estimated_minutes=120,
            due_date=date(2026, 2, 20),
            priority=TaskPriority.HIGH,
            tags=["开发", "测试"]
        )

        assert task.title == "编写测试用例"
        assert task.estimated_minutes == 120
        assert task.due_date == date(2026, 2, 20)
        assert task.priority == TaskPriority.HIGH

    def test_task_due_date_string_parsing(self):
        """测试截止日期字符串解析"""
        task = Task(
            title="任务",
            due_date="2026-02-20"
        )

        assert task.due_date == date(2026, 2, 20)

    def test_task_mark_completed(self):
        """测试标记任务完成"""
        task = Task(title="待办事项")
        assert task.status == TaskStatus.TODO

        task.mark_completed()

        assert task.status == TaskStatus.COMPLETED
        assert task.completed_at is not None
        assert task.is_completed

    def test_task_mark_cancelled(self):
        """测试取消任务"""
        task = Task(title="待办事项")
        task.mark_cancelled()

        assert task.status == TaskStatus.CANCELLED

    def test_task_schedule(self):
        """测试任务时间安排"""
        task = Task(title="开发任务", estimated_minutes=90)
        assert not task.is_scheduled

        start = datetime(2026, 2, 17, 14, 0)
        end = start + timedelta(minutes=90)

        task.schedule(start, end)

        assert task.is_scheduled
        assert task.scheduled_start == start
        assert task.scheduled_end == end
        assert task.status == TaskStatus.IN_PROGRESS

    def test_task_unschedule(self):
        """测试取消任务时间安排"""
        task = Task(title="开发任务")
        start = datetime(2026, 2, 17, 14, 0)
        task.schedule(start, start + timedelta(hours=1))

        task.unschedule()

        assert not task.is_scheduled
        assert task.scheduled_start is None
        assert task.status == TaskStatus.TODO

    def test_task_is_overdue(self):
        """测试任务是否过期"""
        # 无截止日期的任务
        task1 = Task(title="无截止日期")
        assert not task1.is_overdue

        # 已过期的任务
        task2 = Task(
            title="已过期",
            due_date=date(2026, 2, 1)
        )
        assert task2.is_overdue

        # 未过期的任务
        task3 = Task(
            title="未过期",
            due_date=date(2026, 2, 20)
        )
        assert not task3.is_overdue

        # 已完成的任务不算过期
        task4 = Task(
            title="已完成",
            due_date=date(2026, 2, 1)
        )
        task4.mark_completed()
        assert not task4.is_overdue

    def test_task_estimated_hours(self):
        """测试任务预估小时数"""
        task = Task(title="任务", estimated_minutes=90)
        assert task.estimated_hours == 1.5

    def test_task_str_representation(self):
        """测试任务字符串表示"""
        task = Task(
            id="abc12345",
            title="编写代码",
            estimated_minutes=60,
            due_date=date(2026, 2, 20)
        )

        result = str(task)
        assert "编写代码" in result
        assert "60分钟" in result


class TestTimeSlot:
    """TimeSlot 模型测试"""

    def test_create_time_slot(self):
        """测试创建时间段"""
        start = datetime(2026, 2, 17, 9, 0)
        end = start + timedelta(hours=2)

        slot = TimeSlot(start_time=start, end_time=end)

        assert slot.start_time == start
        assert slot.end_time == end
        assert slot.slot_type == SlotType.FREE
        assert slot.duration_minutes == 120

    def test_time_slot_contains(self):
        """测试时间包含检查"""
        start = datetime(2026, 2, 17, 9, 0)
        end = start + timedelta(hours=2)

        slot = TimeSlot(start_time=start, end_time=end)

        assert slot.contains(start)
        assert slot.contains(start + timedelta(hours=1))
        assert not slot.contains(end)  # end 不包含在内
        assert not slot.contains(start - timedelta(minutes=1))

    def test_time_slot_overlaps(self):
        """测试时间重叠检测"""
        base = datetime(2026, 2, 17, 10, 0)

        slot1 = TimeSlot(
            start_time=base,
            end_time=base + timedelta(hours=2)
        )

        # 重叠
        slot2 = TimeSlot(
            start_time=base + timedelta(hours=1),
            end_time=base + timedelta(hours=3)
        )
        assert slot1.overlaps_with(slot2)

        # 不重叠（紧邻）
        slot3 = TimeSlot(
            start_time=base + timedelta(hours=2),
            end_time=base + timedelta(hours=3)
        )
        assert not slot1.overlaps_with(slot3)

    def test_time_slot_can_fit(self):
        """测试是否可容纳指定时长"""
        start = datetime(2026, 2, 17, 9, 0)
        slot = TimeSlot(
            start_time=start,
            end_time=start + timedelta(hours=2)
        )

        assert slot.can_fit(60)
        assert slot.can_fit(120)
        assert not slot.can_fit(150)

    def test_time_slot_cannot_fit_if_busy(self):
        """测试忙碌时间段不能容纳任务"""
        start = datetime(2026, 2, 17, 9, 0)
        slot = TimeSlot(
            start_time=start,
            end_time=start + timedelta(hours=2),
            slot_type=SlotType.BUSY
        )

        assert not slot.can_fit(60)

    def test_time_slot_split(self):
        """测试时间段分割"""
        start = datetime(2026, 2, 17, 9, 0)
        slot = TimeSlot(
            start_time=start,
            end_time=start + timedelta(hours=2)
        )

        task_slot, remaining = slot.split_for_task(60)

        assert task_slot.duration_minutes == 60
        assert task_slot.start_time == start
        assert task_slot.slot_type == SlotType.BUSY

        assert remaining is not None
        assert remaining.duration_minutes == 60
        assert remaining.start_time == start + timedelta(hours=1)
        assert remaining.slot_type == SlotType.FREE

    def test_time_slot_split_no_remaining(self):
        """测试时间段分割（无剩余）"""
        start = datetime(2026, 2, 17, 9, 0)
        slot = TimeSlot(
            start_time=start,
            end_time=start + timedelta(hours=1)
        )

        task_slot, remaining = slot.split_for_task(60)

        assert task_slot.duration_minutes == 60
        assert remaining is None

    def test_time_slot_intersect(self):
        """测试时间段交集"""
        base = datetime(2026, 2, 17, 10, 0)

        slot1 = TimeSlot(
            start_time=base,
            end_time=base + timedelta(hours=3)
        )
        slot2 = TimeSlot(
            start_time=base + timedelta(hours=1),
            end_time=base + timedelta(hours=4)
        )

        intersection = slot1.intersect(slot2)

        assert intersection is not None
        assert intersection.start_time == base + timedelta(hours=1)
        assert intersection.end_time == base + timedelta(hours=3)

    def test_time_slot_no_intersect(self):
        """测试无交集的时间段"""
        base = datetime(2026, 2, 17, 10, 0)

        slot1 = TimeSlot(
            start_time=base,
            end_time=base + timedelta(hours=1)
        )
        slot2 = TimeSlot(
            start_time=base + timedelta(hours=2),
            end_time=base + timedelta(hours=3)
        )

        intersection = slot1.intersect(slot2)
        assert intersection is None

    def test_time_slot_merge(self):
        """测试时间段合并"""
        base = datetime(2026, 2, 17, 10, 0)

        slot1 = TimeSlot(
            start_time=base,
            end_time=base + timedelta(hours=2)
        )
        slot2 = TimeSlot(
            start_time=base + timedelta(hours=2),
            end_time=base + timedelta(hours=4)
        )

        merged = slot1.merge(slot2)

        assert merged is not None
        assert merged.start_time == base
        assert merged.end_time == base + timedelta(hours=4)

    def test_time_slot_merge_overlapping(self):
        """测试重叠时间段合并"""
        base = datetime(2026, 2, 17, 10, 0)

        slot1 = TimeSlot(
            start_time=base,
            end_time=base + timedelta(hours=3)
        )
        slot2 = TimeSlot(
            start_time=base + timedelta(hours=2),
            end_time=base + timedelta(hours=4)
        )

        merged = slot1.merge(slot2)

        assert merged is not None
        assert merged.start_time == base
        assert merged.end_time == base + timedelta(hours=4)

    def test_time_slot_cannot_merge_different_types(self):
        """测试不同类型的时间段不能合并"""
        base = datetime(2026, 2, 17, 10, 0)

        slot1 = TimeSlot(
            start_time=base,
            end_time=base + timedelta(hours=2),
            slot_type=SlotType.FREE
        )
        slot2 = TimeSlot(
            start_time=base + timedelta(hours=2),
            end_time=base + timedelta(hours=4),
            slot_type=SlotType.BUSY
        )

        merged = slot1.merge(slot2)
        assert merged is None

    def test_create_sleep_slots(self):
        """测试创建睡眠时间段"""
        base = datetime(2026, 2, 17, 0, 0)

        sleep_slots = create_sleep_slots(
            base,
            sleep_start_hour=23,
            sleep_end_hour=8
        )

        assert len(sleep_slots) == 1
        sleep_slot = sleep_slots[0]

        assert sleep_slot.slot_type == SlotType.SLEEP
        assert sleep_slot.title == "睡眠时间"
        assert sleep_slot.start_time.hour == 23
        assert sleep_slot.end_time.hour == 8

    def test_time_slot_str_representation(self):
        """测试时间段字符串表示"""
        start = datetime(2026, 2, 17, 9, 0)
        slot = TimeSlot(
            start_time=start,
            end_time=start + timedelta(hours=2),
            slot_type=SlotType.FREE
        )

        result = str(slot)
        assert "free" in result
        assert "120分钟" in result


class TestModelSerialization:
    """模型序列化测试"""

    def test_event_json_serialization(self):
        """测试事件 JSON 序列化"""
        start = datetime(2026, 2, 17, 14, 0)
        event = Event(
            title="会议",
            start_time=start,
            end_time=start + timedelta(hours=1)
        )

        # 转换为字典
        data = event.model_dump()
        assert "id" in data
        assert data["title"] == "会议"

        # 从字典恢复
        restored = Event(**data)
        assert restored.title == event.title
        assert restored.start_time == event.start_time

    def test_task_json_serialization(self):
        """测试任务 JSON 序列化"""
        task = Task(
            title="任务",
            estimated_minutes=90,
            due_date=date(2026, 2, 20)
        )

        data = task.model_dump()
        assert data["title"] == "任务"
        assert data["estimated_minutes"] == 90

        restored = Task(**data)
        assert restored.title == task.title
        assert restored.due_date == task.due_date

    def test_time_slot_json_serialization(self):
        """测试时间段 JSON 序列化"""
        start = datetime(2026, 2, 17, 9, 0)
        slot = TimeSlot(
            start_time=start,
            end_time=start + timedelta(hours=2),
            slot_type=SlotType.FREE
        )

        data = slot.model_dump()
        assert data["slot_type"] == SlotType.FREE.value

        restored = TimeSlot(**data)
        assert restored.slot_type == SlotType.FREE
