"""Automation services."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, Iterable, Optional

from agent_core.models import Event, EventPriority, Task, TaskPriority
from agent_core.storage.json_repository import EventRepository, TaskRepository

from .connectors import BaseConnector
from .event_bus import AsyncEventBus
from .repositories import (
    AutomationPolicyRepository,
    DigestRepository,
    ExternalItemRepository,
    NotificationOutboxRepository,
    SyncCursorRepository,
)
from .types import (
    Digest,
    ExternalItem,
    NotificationOutbox,
    NotificationStatus,
    SyncCursor,
)


class SyncIngestionService:
    def __init__(
        self,
        connectors: Iterable[BaseConnector],
        external_repo: ExternalItemRepository,
        cursor_repo: SyncCursorRepository,
        event_bus: AsyncEventBus,
    ):
        self._connectors: Dict[str, BaseConnector] = {
            connector.source_type: connector for connector in connectors
        }
        self._external_repo = external_repo
        self._cursor_repo = cursor_repo
        self._event_bus = event_bus

    async def run_source(self, source_type: str, account_id: str = "default") -> dict:
        connector = self._connectors.get(source_type)
        if connector is None:
            return {
                "source": source_type,
                "error": "CONNECTOR_NOT_FOUND",
                "created": 0,
                "updated": 0,
            }

        cursor_id = f"{source_type}:{account_id}"
        cursor = self._cursor_repo.get(cursor_id)
        since_cursor = cursor.cursor if cursor else None

        result = await connector.fetch(since_cursor=since_cursor, account_id=account_id)

        created = 0
        updated = 0
        for item in result.items:
            model = ExternalItem(
                source_type=source_type,
                external_id=item.external_id,
                fingerprint=item.fingerprint,
                occurred_at=item.occurred_at,
                raw_payload=item.raw_payload,
                normalized_payload=item.normalized_payload,
            )
            existing = self._external_repo.get(model.id)
            if existing is None:
                self._external_repo.create(model)
                created += 1
            else:
                model.created_at = existing.created_at
                self._external_repo.update(model)
                updated += 1

            await self._event_bus.publish(
                "external.item.upserted",
                {
                    "source_type": source_type,
                    "external_item_id": model.id,
                    "normalized_payload": model.normalized_payload,
                },
            )

        next_cursor = result.next_cursor or datetime.now().isoformat()
        cursor_model = SyncCursor(
            source_type=source_type, account_id=account_id, cursor=next_cursor
        )
        if cursor is None:
            self._cursor_repo.create(cursor_model)
        else:
            self._cursor_repo.update(cursor_model)

        return {
            "source": source_type,
            "created": created,
            "updated": updated,
            "next_cursor": next_cursor,
        }


class NormalizationWriteService:
    def __init__(
        self,
        event_repo: Optional[EventRepository],
        task_repo: Optional[TaskRepository],
        policy_repo: AutomationPolicyRepository,
        event_bus: AsyncEventBus,
    ):
        self._event_repo = event_repo or EventRepository()
        self._task_repo = task_repo or TaskRepository()
        self._policy_repo = policy_repo
        self._event_bus = event_bus

    async def handle_external_item(self, event: dict) -> None:
        payload = event.get("payload", {})
        normalized = payload.get("normalized_payload") or {}
        source_type = payload.get("source_type", "unknown")

        policy = self._policy_repo.get_default()
        if not policy.auto_write_enabled:
            return

        kind = normalized.get("kind")
        if kind == "event":
            await self._write_event(source_type, normalized)
        elif kind == "task":
            await self._write_task(source_type, normalized)

    @staticmethod
    def _truncate(text: str | None, max_len: int = 2000) -> str | None:
        if text and len(text) > max_len:
            return text[:max_len]
        return text

    async def _write_event(self, source_type: str, payload: dict) -> None:
        title = payload.get("title") or "[自动化] 未命名事件"
        start_raw = payload.get("start_time")
        end_raw = payload.get("end_time")
        if not start_raw or not end_raw:
            return

        start_time = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))

        event = Event(
            title=title,
            description=self._truncate(payload.get("description")),
            start_time=start_time,
            end_time=end_time,
            priority=EventPriority(payload.get("priority", "medium")),
            tags=payload.get("tags", []),
            source="system",
            event_type="normal",
            metadata={"source_type": source_type, "automation": True},
            origin_ref=payload.get("origin_ref"),
        )
        self._event_repo.create(event)
        await self._event_bus.publish(
            "schedule.changed", {"event_id": event.id, "source_type": source_type}
        )

    async def _write_task(self, source_type: str, payload: dict) -> None:
        title = payload.get("title") or "[自动化] 未命名任务"
        due_date = payload.get("due_date")
        task = Task(
            title=title,
            description=self._truncate(payload.get("description")),
            estimated_minutes=int(payload.get("estimated_minutes", 30)),
            due_date=due_date,
            priority=TaskPriority(payload.get("priority", "medium")),
            tags=payload.get("tags", []),
            source="system",
            metadata={"source_type": source_type, "automation": True},
            origin_ref=payload.get("origin_ref"),
        )
        self._task_repo.create(task)
        await self._event_bus.publish(
            "task.changed", {"task_id": task.id, "source_type": source_type}
        )


class SummaryService:
    def __init__(
        self,
        digest_repo: DigestRepository,
        event_repo: Optional[EventRepository],
        task_repo: Optional[TaskRepository],
        event_bus: AsyncEventBus,
    ):
        self._digest_repo = digest_repo
        self._event_repo = event_repo or EventRepository()
        self._task_repo = task_repo or TaskRepository()
        self._event_bus = event_bus

    async def handle_summary_requested(self, event: dict) -> None:
        payload = event.get("payload", {})
        digest_type = payload.get("digest_type", "daily")
        if digest_type == "weekly":
            digest = self.generate_weekly_digest()
            await self._event_bus.publish(
                "weekly_digest.ready", {"digest_id": digest.id}
            )
        else:
            digest = self.generate_daily_digest()
            await self._event_bus.publish(
                "daily_digest.ready", {"digest_id": digest.id}
            )

    def generate_daily_digest(self, day: Optional[date] = None) -> Digest:
        target_day = day or date.today()
        events = self._event_repo.get_by_date(target_day)
        todo_tasks = self._task_repo.get_todo()
        overdue_tasks = self._task_repo.get_overdue()

        # 基础统计
        highlights = [
            f"今日事件 {len(events)} 个",
            f"待办任务 {len(todo_tasks)} 个",
            f"逾期任务 {len(overdue_tasks)} 个",
        ]

        # 选出今日较为关键的事件（按优先级 + 开始时间排序，最多 5 条）
        priority_order = {
            EventPriority.URGENT: 3,
            EventPriority.HIGH: 2,
            EventPriority.MEDIUM: 1,
            EventPriority.LOW: 0,
        }
        important_events = sorted(
            events,
            key=lambda e: (-priority_order.get(e.priority, 0), e.start_time),
        )[:5]

        # 选出紧急/重要任务（含逾期 + 即将到期，最多 5 条）
        all_candidate_tasks = todo_tasks + overdue_tasks
        task_priority_order = {
            TaskPriority.URGENT: 3,
            TaskPriority.HIGH: 2,
            TaskPriority.MEDIUM: 1,
            TaskPriority.LOW: 0,
        }
        candidates_with_due = [t for t in all_candidate_tasks if t.due_date is not None]
        urgent_tasks = sorted(
            candidates_with_due,
            key=lambda t: (
                -task_priority_order.get(t.priority, 0),
                t.due_date,
            ),
        )[:5]

        # 组装 markdown 摘要内容
        content_lines: list[str] = []

        # 总结性一句话
        summary_line = (
            f"今天共有 {len(events)} 个事件，"
            f"{len(todo_tasks)} 个待办任务，其中 {len(overdue_tasks)} 个已逾期。"
        )
        content_lines.append(f"- {summary_line}")

        # 基础统计条目（保留原来的高层统计，方便快速扫一眼）
        for item in highlights:
            content_lines.append(f"- {item}")

        # 今日关键事件
        if important_events:
            content_lines.append("")
            content_lines.append("## 今日关键事件")
            for e in important_events:
                time_str = (
                    f"{e.start_time.strftime('%H:%M')} - {e.end_time.strftime('%H:%M')}"
                )
                tags = ", ".join(e.tags[:3]) if e.tags else ""
                tags_part = f"，标签：{tags}" if tags else ""
                content_lines.append(
                    f"- {time_str} · {e.title}（优先级：{e.priority.value}{tags_part}）"
                )

        # 紧急/重要任务
        if urgent_tasks:
            content_lines.append("")
            content_lines.append("## 紧急/重要任务")
            for t in urgent_tasks:
                due_str = t.due_date.isoformat() if t.due_date else "未设置截止日期"
                content_lines.append(
                    f"- [截止 {due_str}] {t.title}"
                    f"（优先级：{t.priority.value}，预计 {t.estimated_minutes} 分钟）"
                )

        content = "\n".join(content_lines)
        digest = Digest(
            digest_type="daily",
            period_start=target_day,
            period_end=target_day,
            content_md=content,
            highlights=highlights,
        )
        self._digest_repo.create(digest)
        return digest

    def generate_weekly_digest(self, start_day: Optional[date] = None) -> Digest:
        today = date.today()
        week_start = start_day or (today - timedelta(days=today.weekday()))
        week_end = week_start + timedelta(days=6)

        # 一周内的事件总数与列表
        week_events: list[Event] = []
        for i in range(7):
            day = week_start + timedelta(days=i)
            day_events = self._event_repo.get_by_date(day)
            week_events.extend(day_events)
        total_events = len(week_events)

        todo_tasks = self._task_repo.get_todo()
        completed_tasks = self._task_repo.get_completed()

        # 限定在本周范围内的任务
        week_todo = [
            t
            for t in todo_tasks
            if t.due_date is not None and week_start <= t.due_date <= week_end
        ]
        week_completed = [
            t
            for t in completed_tasks
            if t.due_date is not None and week_start <= t.due_date <= week_end
        ]

        highlights = [
            f"本周事件总数 {total_events} 个",
            f"本周内进行中/待办任务 {len(week_todo)} 个",
            f"本周内已完成任务 {len(week_completed)} 个",
        ]

        # 选出本周较为关键的事件（按优先级 + 开始时间排序，最多 5 条）
        priority_order = {
            EventPriority.URGENT: 3,
            EventPriority.HIGH: 2,
            EventPriority.MEDIUM: 1,
            EventPriority.LOW: 0,
        }
        important_events = sorted(
            week_events,
            key=lambda e: (-priority_order.get(e.priority, 0), e.start_time),
        )[:5]

        # 选出本周紧急/重要任务（按优先级 + 截止日期排序，最多 5 条）
        task_priority_order = {
            TaskPriority.URGENT: 3,
            TaskPriority.HIGH: 2,
            TaskPriority.MEDIUM: 1,
            TaskPriority.LOW: 0,
        }
        important_tasks = sorted(
            week_todo,
            key=lambda t: (
                -task_priority_order.get(t.priority, 0),
                t.due_date,
            ),
        )[:5]

        content_lines: list[str] = []
        summary_line = (
            f"本周时间范围：{week_start.isoformat()} ~ {week_end.isoformat()}，"
            f"共有 {total_events} 个事件、{len(week_todo)} 个待办任务，"
            f"{len(week_completed)} 个任务已完成。"
        )
        content_lines.append(f"- {summary_line}")

        for item in highlights:
            content_lines.append(f"- {item}")

        if important_events:
            content_lines.append("")
            content_lines.append("## 本周关键事件")
            for e in important_events:
                day_str = e.start_time.strftime("%Y-%m-%d")
                time_str = (
                    f"{e.start_time.strftime('%H:%M')} - {e.end_time.strftime('%H:%M')}"
                )
                tags = ", ".join(e.tags[:3]) if e.tags else ""
                tags_part = f"，标签：{tags}" if tags else ""
                content_lines.append(
                    f"- {day_str} {time_str} · {e.title}（优先级：{e.priority.value}{tags_part}）"
                )

        if important_tasks:
            content_lines.append("")
            content_lines.append("## 本周重点任务")
            for t in important_tasks:
                due_str = t.due_date.isoformat() if t.due_date else "未设置截止日期"
                content_lines.append(
                    f"- [截止 {due_str}] {t.title}"
                    f"（优先级：{t.priority.value}，预计 {t.estimated_minutes} 分钟）"
                )

        content = "\n".join(content_lines)
        digest = Digest(
            digest_type="weekly",
            period_start=week_start,
            period_end=week_end,
            content_md=content,
            highlights=highlights,
        )
        self._digest_repo.create(digest)
        return digest


class NotificationOutboxService:
    def __init__(self, outbox_repo: NotificationOutboxRepository):
        self._outbox_repo = outbox_repo

    async def handle_digest_ready(self, event: dict) -> None:
        payload = event.get("payload", {})
        digest_id = payload.get("digest_id")
        template = "digest_ready"
        outbox = NotificationOutbox(
            channel="in_app",
            target="default",
            template=template,
            payload={"digest_id": digest_id},
            status=NotificationStatus.SENT,
            sent_at=datetime.now(),
        )
        self._outbox_repo.create(outbox)

    def list_notifications(
        self, limit: int = 20, status: Optional[str] = None
    ) -> list[NotificationOutbox]:
        parsed_status = None
        if status:
            try:
                parsed_status = NotificationStatus(status)
            except ValueError:
                parsed_status = None
        return self._outbox_repo.list_by_status(parsed_status, limit)

    def ack_notification(self, outbox_id: str) -> Optional[NotificationOutbox]:
        outbox = self._outbox_repo.get(outbox_id)
        if outbox is None:
            return None
        outbox.status = NotificationStatus.ACKED
        outbox.acked_at = datetime.now()
        self._outbox_repo.update(outbox)
        return outbox
