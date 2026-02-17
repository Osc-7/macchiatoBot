"""
测试 JSON 存储仓库

测试 EventRepository 和 TaskRepository 的 CRUD 操作和查询功能。
"""

import json
import os
import tempfile
from datetime import datetime, date, timedelta
from pathlib import Path

import pytest

from schedule_agent.models import Event, Task, EventStatus, EventPriority, TaskStatus, TaskPriority
from schedule_agent.storage import JSONRepository, EventRepository, TaskRepository


# ============ 测试夹具 ============

@pytest.fixture
def temp_dir():
    """创建临时目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def event_repo(temp_dir):
    """创建 EventRepository 实例"""
    file_path = Path(temp_dir) / "events.json"
    return EventRepository(file_path)


@pytest.fixture
def task_repo(temp_dir):
    """创建 TaskRepository 实例"""
    file_path = Path(temp_dir) / "tasks.json"
    return TaskRepository(file_path)


@pytest.fixture
def sample_event():
    """创建示例事件"""
    return Event(
        id="evt001",
        title="团队周会",
        description="每周团队例会",
        start_time=datetime(2026, 2, 18, 14, 0),
        end_time=datetime(2026, 2, 18, 15, 0),
        location="会议室A",
        status=EventStatus.SCHEDULED,
        priority=EventPriority.HIGH,
        tags=["会议", "团队"],
        reminders=[15]
    )


@pytest.fixture
def sample_task():
    """创建示例任务"""
    return Task(
        id="tsk001",
        title="完成项目报告",
        description="撰写Q1项目进展报告",
        estimated_minutes=120,
        due_date=date(2026, 2, 20),
        status=TaskStatus.TODO,
        priority=TaskPriority.HIGH,
        tags=["报告", "项目"]
    )


# ============ JSONRepository 基础测试 ============

class TestJSONRepositoryBasics:
    """测试 JSONRepository 基础功能"""

    def test_create_file_on_init(self, temp_dir):
        """测试初始化时自动创建文件"""
        file_path = Path(temp_dir) / "test.json"
        repo = JSONRepository(file_path, Event)
        assert os.path.exists(file_path)

    def test_create_and_get(self, event_repo, sample_event):
        """测试创建和获取"""
        # 创建
        created = event_repo.create(sample_event)
        assert created.id == sample_event.id
        assert created.title == sample_event.title

        # 获取
        retrieved = event_repo.get(sample_event.id)
        assert retrieved is not None
        assert retrieved.id == sample_event.id
        assert retrieved.title == sample_event.title

    def test_get_nonexistent(self, event_repo):
        """测试获取不存在的记录"""
        result = event_repo.get("nonexistent")
        assert result is None

    def test_update(self, event_repo, sample_event):
        """测试更新"""
        event_repo.create(sample_event)

        # 更新
        sample_event.title = "更新后的标题"
        updated = event_repo.update(sample_event)

        assert updated is not None
        assert updated.title == "更新后的标题"

        # 验证持久化
        retrieved = event_repo.get(sample_event.id)
        assert retrieved.title == "更新后的标题"

    def test_update_nonexistent(self, event_repo, sample_event):
        """测试更新不存在的记录"""
        result = event_repo.update(sample_event)
        assert result is None

    def test_delete(self, event_repo, sample_event):
        """测试删除"""
        event_repo.create(sample_event)

        # 删除
        success = event_repo.delete(sample_event.id)
        assert success is True

        # 验证已删除
        retrieved = event_repo.get(sample_event.id)
        assert retrieved is None

    def test_delete_nonexistent(self, event_repo):
        """测试删除不存在的记录"""
        success = event_repo.delete("nonexistent")
        assert success is False

    def test_get_all(self, event_repo):
        """测试获取所有记录"""
        # 创建多个事件
        for i in range(3):
            event = Event(
                id=f"evt{i:03d}",
                title=f"事件 {i}",
                start_time=datetime(2026, 2, 18, 10 + i, 0),
                end_time=datetime(2026, 2, 18, 11 + i, 0),
            )
            event_repo.create(event)

        all_events = event_repo.get_all()
        assert len(all_events) == 3

    def test_count(self, event_repo):
        """测试计数"""
        assert event_repo.count() == 0

        for i in range(5):
            event = Event(
                id=f"evt{i:03d}",
                title=f"事件 {i}",
                start_time=datetime(2026, 2, 18, 10, 0),
                end_time=datetime(2026, 2, 18, 11, 0),
            )
            event_repo.create(event)

        assert event_repo.count() == 5

    def test_clear(self, event_repo, sample_event):
        """测试清空"""
        event_repo.create(sample_event)
        assert event_repo.count() == 1

        event_repo.clear()
        assert event_repo.count() == 0


# ============ EventRepository 测试 ============

class TestEventRepository:
    """测试 EventRepository 专用功能"""

    def test_get_by_date_range(self, event_repo):
        """测试按时间范围查询"""
        # 创建不同时间的事件
        events = [
            Event(
                id="evt001",
                title="上午会议",
                start_time=datetime(2026, 2, 18, 9, 0),
                end_time=datetime(2026, 2, 18, 10, 0),
            ),
            Event(
                id="evt002",
                title="下午会议",
                start_time=datetime(2026, 2, 18, 14, 0),
                end_time=datetime(2026, 2, 18, 15, 0),
            ),
            Event(
                id="evt003",
                title="明天会议",
                start_time=datetime(2026, 2, 19, 10, 0),
                end_time=datetime(2026, 2, 19, 11, 0),
            ),
        ]
        for e in events:
            event_repo.create(e)

        # 查询 2月18日的事件
        start = datetime(2026, 2, 18, 0, 0)
        end = datetime(2026, 2, 18, 23, 59)
        result = event_repo.get_by_date_range(start, end)

        assert len(result) == 2
        assert all(e.id in ["evt001", "evt002"] for e in result)

    def test_get_by_date(self, event_repo):
        """测试按日期查询"""
        target_date = date(2026, 2, 18)

        events = [
            Event(
                id="evt001",
                title="会议1",
                start_time=datetime(2026, 2, 18, 9, 0),
                end_time=datetime(2026, 2, 18, 10, 0),
            ),
            Event(
                id="evt002",
                title="会议2",
                start_time=datetime(2026, 2, 19, 10, 0),
                end_time=datetime(2026, 2, 19, 11, 0),
            ),
        ]
        for e in events:
            event_repo.create(e)

        result = event_repo.get_by_date(target_date)
        assert len(result) == 1
        assert result[0].id == "evt001"

    def test_get_by_status(self, event_repo):
        """测试按状态查询"""
        events = [
            Event(
                id="evt001",
                title="已安排",
                start_time=datetime(2026, 2, 18, 9, 0),
                end_time=datetime(2026, 2, 18, 10, 0),
                status=EventStatus.SCHEDULED,
            ),
            Event(
                id="evt002",
                title="已完成",
                start_time=datetime(2026, 2, 18, 10, 0),
                end_time=datetime(2026, 2, 18, 11, 0),
                status=EventStatus.COMPLETED,
            ),
            Event(
                id="evt003",
                title="已取消",
                start_time=datetime(2026, 2, 18, 11, 0),
                end_time=datetime(2026, 2, 18, 12, 0),
                status=EventStatus.CANCELLED,
            ),
        ]
        for e in events:
            event_repo.create(e)

        # 查询已完成
        completed = event_repo.get_by_status(EventStatus.COMPLETED)
        assert len(completed) == 1
        assert completed[0].id == "evt002"

    def test_find_conflicts(self, event_repo):
        """测试查找冲突"""
        # 创建一个事件
        event1 = Event(
            id="evt001",
            title="会议1",
            start_time=datetime(2026, 2, 18, 10, 0),
            end_time=datetime(2026, 2, 18, 11, 0),
        )
        event_repo.create(event1)

        # 创建一个有冲突的事件（不保存）
        conflict_event = Event(
            id="evt002",
            title="冲突会议",
            start_time=datetime(2026, 2, 18, 10, 30),
            end_time=datetime(2026, 2, 18, 11, 30),
        )

        conflicts = event_repo.find_conflicts(conflict_event)
        assert len(conflicts) == 1
        assert conflicts[0].id == "evt001"

    def test_find_conflicts_exclude_self(self, event_repo):
        """测试查找冲突时排除自身"""
        event = Event(
            id="evt001",
            title="会议",
            start_time=datetime(2026, 2, 18, 10, 0),
            end_time=datetime(2026, 2, 18, 11, 0),
        )
        event_repo.create(event)

        # 更新同一事件时不应该与自身冲突
        conflicts = event_repo.find_conflicts(event, exclude_id="evt001")
        assert len(conflicts) == 0

    def test_no_conflict_with_cancelled(self, event_repo):
        """测试取消的事件不产生冲突"""
        cancelled_event = Event(
            id="evt001",
            title="已取消",
            start_time=datetime(2026, 2, 18, 10, 0),
            end_time=datetime(2026, 2, 18, 11, 0),
            status=EventStatus.CANCELLED,
        )
        event_repo.create(cancelled_event)

        new_event = Event(
            id="evt002",
            title="新事件",
            start_time=datetime(2026, 2, 18, 10, 30),
            end_time=datetime(2026, 2, 18, 11, 30),
        )

        conflicts = event_repo.find_conflicts(new_event)
        assert len(conflicts) == 0

    def test_search_by_title(self, event_repo):
        """测试按标题搜索"""
        events = [
            Event(
                id="evt001",
                title="团队周会",
                start_time=datetime(2026, 2, 18, 10, 0),
                end_time=datetime(2026, 2, 18, 11, 0),
            ),
            Event(
                id="evt002",
                title="项目评审",
                start_time=datetime(2026, 2, 18, 14, 0),
                end_time=datetime(2026, 2, 18, 15, 0),
            ),
        ]
        for e in events:
            event_repo.create(e)

        result = event_repo.search("周会")
        assert len(result) == 1
        assert result[0].id == "evt001"

    def test_search_by_tag(self, event_repo):
        """测试按标签搜索"""
        events = [
            Event(
                id="evt001",
                title="会议1",
                start_time=datetime(2026, 2, 18, 10, 0),
                end_time=datetime(2026, 2, 18, 11, 0),
                tags=["重要", "团队"],
            ),
            Event(
                id="evt002",
                title="会议2",
                start_time=datetime(2026, 2, 18, 14, 0),
                end_time=datetime(2026, 2, 18, 15, 0),
                tags=["临时"],
            ),
        ]
        for e in events:
            event_repo.create(e)

        result = event_repo.search("重要")
        assert len(result) == 1
        assert result[0].id == "evt001"

    def test_get_upcoming(self, event_repo):
        """测试获取未来事件"""
        now = datetime.now()

        events = [
            Event(
                id="evt001",
                title="过去事件",
                start_time=now - timedelta(days=2),
                end_time=now - timedelta(days=2, hours=-1),
            ),
            Event(
                id="evt002",
                title="未来事件",
                start_time=now + timedelta(days=2),
                end_time=now + timedelta(days=2, hours=1),
            ),
        ]
        for e in events:
            event_repo.create(e)

        result = event_repo.get_upcoming(days=7)
        assert len(result) == 1
        assert result[0].id == "evt002"


# ============ TaskRepository 测试 ============

class TestTaskRepository:
    """测试 TaskRepository 专用功能"""

    def test_get_by_status(self, task_repo):
        """测试按状态查询"""
        tasks = [
            Task(id="tsk001", title="待办任务", status=TaskStatus.TODO),
            Task(id="tsk002", title="已完成任务", status=TaskStatus.COMPLETED),
        ]
        for t in tasks:
            task_repo.create(t)

        todo_tasks = task_repo.get_by_status(TaskStatus.TODO)
        assert len(todo_tasks) == 1
        assert todo_tasks[0].id == "tsk001"

    def test_get_todo(self, task_repo):
        """测试获取待办任务"""
        tasks = [
            Task(id="tsk001", title="待办1", status=TaskStatus.TODO),
            Task(id="tsk002", title="待办2", status=TaskStatus.TODO),
            Task(id="tsk003", title="已完成", status=TaskStatus.COMPLETED),
        ]
        for t in tasks:
            task_repo.create(t)

        result = task_repo.get_todo()
        assert len(result) == 2

    def test_get_completed(self, task_repo):
        """测试获取已完成任务"""
        tasks = [
            Task(id="tsk001", title="待办", status=TaskStatus.TODO),
            Task(id="tsk002", title="已完成1", status=TaskStatus.COMPLETED),
            Task(id="tsk003", title="已完成2", status=TaskStatus.COMPLETED),
        ]
        for t in tasks:
            task_repo.create(t)

        result = task_repo.get_completed()
        assert len(result) == 2

    def test_get_overdue(self, task_repo):
        """测试获取过期任务"""
        tasks = [
            Task(
                id="tsk001",
                title="过期任务",
                due_date=date.today() - timedelta(days=1),
                status=TaskStatus.TODO,
            ),
            Task(
                id="tsk002",
                title="未来任务",
                due_date=date.today() + timedelta(days=1),
                status=TaskStatus.TODO,
            ),
            Task(
                id="tsk003",
                title="过期但已完成",
                due_date=date.today() - timedelta(days=1),
                status=TaskStatus.COMPLETED,
            ),
        ]
        for t in tasks:
            task_repo.create(t)

        result = task_repo.get_overdue()
        assert len(result) == 1
        assert result[0].id == "tsk001"

    def test_get_due_today(self, task_repo):
        """测试获取今天截止的任务"""
        tasks = [
            Task(
                id="tsk001",
                title="今天截止",
                due_date=date.today(),
                status=TaskStatus.TODO,
            ),
            Task(
                id="tsk002",
                title="明天截止",
                due_date=date.today() + timedelta(days=1),
                status=TaskStatus.TODO,
            ),
        ]
        for t in tasks:
            task_repo.create(t)

        result = task_repo.get_due_today()
        assert len(result) == 1
        assert result[0].id == "tsk001"

    def test_get_due_this_week(self, task_repo):
        """测试获取本周截止的任务"""
        tasks = [
            Task(
                id="tsk001",
                title="本周截止",
                due_date=date.today() + timedelta(days=2),
                status=TaskStatus.TODO,
            ),
            Task(
                id="tsk002",
                title="下周截止",
                due_date=date.today() + timedelta(days=10),
                status=TaskStatus.TODO,
            ),
        ]
        for t in tasks:
            task_repo.create(t)

        result = task_repo.get_due_this_week()
        assert len(result) == 1
        assert result[0].id == "tsk001"

    def test_get_scheduled(self, task_repo):
        """测试获取已安排的任务"""
        tasks = [
            Task(
                id="tsk001",
                title="已安排",
                scheduled_start=datetime(2026, 2, 18, 10, 0),
                scheduled_end=datetime(2026, 2, 18, 12, 0),
            ),
            Task(
                id="tsk002",
                title="未安排",
            ),
        ]
        for t in tasks:
            task_repo.create(t)

        result = task_repo.get_scheduled()
        assert len(result) == 1
        assert result[0].id == "tsk001"

    def test_get_unscheduled(self, task_repo):
        """测试获取未安排的任务"""
        tasks = [
            Task(
                id="tsk001",
                title="待办未安排",
                status=TaskStatus.TODO,
            ),
            Task(
                id="tsk002",
                title="已安排",
                status=TaskStatus.IN_PROGRESS,
                scheduled_start=datetime(2026, 2, 18, 10, 0),
                scheduled_end=datetime(2026, 2, 18, 12, 0),
            ),
            Task(
                id="tsk003",
                title="已完成",
                status=TaskStatus.COMPLETED,
            ),
        ]
        for t in tasks:
            task_repo.create(t)

        result = task_repo.get_unscheduled()
        assert len(result) == 1
        assert result[0].id == "tsk001"

    def test_search_by_title(self, task_repo):
        """测试按标题搜索任务"""
        tasks = [
            Task(id="tsk001", title="写项目报告"),
            Task(id="tsk002", title="整理文档"),
        ]
        for t in tasks:
            task_repo.create(t)

        result = task_repo.search("报告")
        assert len(result) == 1
        assert result[0].id == "tsk001"

    def test_search_by_tag(self, task_repo):
        """测试按标签搜索任务"""
        tasks = [
            Task(id="tsk001", title="任务1", tags=["紧急", "项目"]),
            Task(id="tsk002", title="任务2", tags=["日常"]),
        ]
        for t in tasks:
            task_repo.create(t)

        result = task_repo.search("紧急")
        assert len(result) == 1
        assert result[0].id == "tsk001"


# ============ 持久化测试 ============

class TestPersistence:
    """测试数据持久化"""

    def test_event_persistence(self, temp_dir):
        """测试事件持久化"""
        file_path = Path(temp_dir) / "events.json"

        # 创建并保存
        repo1 = EventRepository(file_path)
        event = Event(
            id="evt001",
            title="测试事件",
            start_time=datetime(2026, 2, 18, 10, 0),
            end_time=datetime(2026, 2, 18, 11, 0),
        )
        repo1.create(event)

        # 重新加载
        repo2 = EventRepository(file_path)
        loaded = repo2.get("evt001")

        assert loaded is not None
        assert loaded.title == "测试事件"

    def test_task_persistence(self, temp_dir):
        """测试任务持久化"""
        file_path = Path(temp_dir) / "tasks.json"

        # 创建并保存
        repo1 = TaskRepository(file_path)
        task = Task(
            id="tsk001",
            title="测试任务",
            due_date=date(2026, 2, 20),
        )
        repo1.create(task)

        # 重新加载
        repo2 = TaskRepository(file_path)
        loaded = repo2.get("tsk001")

        assert loaded is not None
        assert loaded.title == "测试任务"
        assert loaded.due_date == date(2026, 2, 20)

    def test_json_format(self, temp_dir):
        """测试 JSON 文件格式"""
        file_path = Path(temp_dir) / "events.json"
        repo = EventRepository(file_path)

        event = Event(
            id="evt001",
            title="测试事件",
            description="事件描述",
            start_time=datetime(2026, 2, 18, 10, 0),
            end_time=datetime(2026, 2, 18, 11, 0),
            tags=["测试"],
        )
        repo.create(event)

        # 读取原始 JSON
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert "evt001" in data
        assert data["evt001"]["title"] == "测试事件"
        assert "tags" in data["evt001"]


# ============ 边界条件测试 ============

class TestEdgeCases:
    """测试边界条件"""

    def test_empty_repository(self, event_repo):
        """测试空仓库"""
        assert event_repo.count() == 0
        assert event_repo.get_all() == []
        assert event_repo.get("any") is None

    def test_special_characters_in_title(self, event_repo):
        """测试标题中的特殊字符"""
        event = Event(
            id="evt001",
            title="会议 \"重要\" & '紧急' <测试>",
            start_time=datetime(2026, 2, 18, 10, 0),
            end_time=datetime(2026, 2, 18, 11, 0),
        )
        event_repo.create(event)

        loaded = event_repo.get("evt001")
        assert loaded.title == "会议 \"重要\" & '紧急' <测试>"

    def test_chinese_characters(self, event_repo):
        """测试中文字符"""
        event = Event(
            id="evt001",
            title="团队周会",
            description="讨论项目进展",
            start_time=datetime(2026, 2, 18, 10, 0),
            end_time=datetime(2026, 2, 18, 11, 0),
            tags=["会议", "团队"],
        )
        event_repo.create(event)

        loaded = event_repo.get("evt001")
        assert loaded.title == "团队周会"
        assert "会议" in loaded.tags

    def test_long_description(self, event_repo):
        """测试长描述"""
        long_desc = "这是一个很长的描述。" * 100  # 1000+ 字符
        event = Event(
            id="evt001",
            title="测试",
            description=long_desc,
            start_time=datetime(2026, 2, 18, 10, 0),
            end_time=datetime(2026, 2, 18, 11, 0),
        )
        event_repo.create(event)

        loaded = event_repo.get("evt001")
        assert loaded.description == long_desc

    def test_task_without_due_date(self, task_repo):
        """测试没有截止日期的任务"""
        task = Task(
            id="tsk001",
            title="无截止日期任务",
        )
        task_repo.create(task)

        loaded = task_repo.get("tsk001")
        assert loaded.due_date is None
        assert loaded.is_overdue is False
