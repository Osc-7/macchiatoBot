"""Canvas 到日程系统的同步逻辑"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Set, Callable, Awaitable

from .client import CanvasClient
from .models import CanvasAssignment, CanvasEvent, SyncResult, now_utc

logger = logging.getLogger(__name__)


# 日程事件创建回调类型
EventCreatorCallback = Callable[[Dict[str, Any]], Awaitable[Optional[str]]]


class CanvasSync:
    """Canvas 同步器
    
    负责将 Canvas 的作业和日历事件同步到日程系统。
    
    Attributes:
        client: Canvas API 客户端
        event_creator: 日程事件创建回调函数
        synced_event_ids: 已同步的事件 ID 集合（防止重复）
        synced_assignments_ids: 已同步的作业 ID 集合
    
    Example:
        >>> # 方式 1: 使用默认同步（需要外部处理事件创建）
        >>> sync = CanvasSync(client)
        >>> result = await sync.sync_to_schedule(days_ahead=60)
        >>>
        >>> # 方式 2: 传入事件创建回调（完整同步）
        >>> async def create_event(event_data: dict) -> str | None:
        ...     return await call_tool("add_event", event_data)
        >>>
        >>> sync = CanvasSync(client, event_creator=create_event)
        >>> result = await sync.sync_to_schedule(days_ahead=60)
    """
    
    def __init__(
        self,
        client: CanvasClient,
        event_creator: Optional[EventCreatorCallback] = None,
    ):
        """初始化同步器
        
        Args:
            client: Canvas API 客户端
            event_creator: 日程事件创建回调函数（可选）
                        如果提供，同步会自动创建日程事件
                        如果不提供，同步只返回事件数据，需要外部处理
        """
        self.client = client
        self.event_creator = event_creator
        self.synced_event_ids: Set[int] = set()
        self.synced_assignments_ids: Set[int] = set()
    
    async def sync_to_schedule(
        self,
        days_ahead: int = 60,
        include_submitted: bool = False,
    ) -> SyncResult:
        """同步 Canvas 事件到日程系统
        
        Args:
            days_ahead: 同步未来多少天的事件
            include_submitted: 是否包含已提交的作业
            
        Returns:
            同步结果对象
            
        Example:
            >>> result = await sync.sync_to_schedule(days_ahead=30)
            >>> print(f"Created: {result.created_count}, Errors: {len(result.errors)}")
        """
        result = SyncResult()
        
        logger.info(f"Starting sync for next {days_ahead} days...")
        
        # 1. 获取所有即将到来的作业
        try:
            assignments = await self.client.get_upcoming_assignments(
                days=days_ahead,
                include_submitted=include_submitted,
            )
            logger.info(f"Found {len(assignments)} upcoming assignments")
        except Exception as e:
            logger.error(f"Failed to get assignments: {e}")
            result.add_error(f"Failed to get assignments: {e}")
            assignments = []
        
        # 2. 获取所有即将到来的日历事件
        try:
            events = await self.client.get_upcoming_events(days=days_ahead)
            logger.info(f"Found {len(events)} upcoming calendar events")
        except Exception as e:
            logger.error(f"Failed to get calendar events: {e}")
            result.add_error(f"Failed to get calendar events: {e}")
            events = []
        
        # 3. 同步作业
        for assignment in assignments:
            try:
                await self._sync_assignment(assignment, result)
            except Exception as e:
                logger.error(
                    f"Failed to sync assignment {assignment.id}: {e}"
                )
                result.add_error(
                    f"Assignment {assignment.name}: {e}"
                )
        
        # 4. 同步日历事件（排除已经是作业的事件）
        assignment_ids = {a.id for a in assignments}
        for event in events:
            # 跳过已经是作业的事件（避免重复）
            if event.id in assignment_ids:
                continue
            
            try:
                await self._sync_calendar_event(event, result)
            except Exception as e:
                logger.error(
                    f"Failed to sync calendar event {event.id}: {e}"
                )
                result.add_error(
                    f"Event {event.title}: {e}"
                )
        
        logger.info(
            f"Sync completed: {result.created_count} created, "
            f"{result.updated_count} updated, "
            f"{result.skipped_count} skipped"
        )
        
        return result
    
    async def _sync_assignment(
        self,
        assignment: CanvasAssignment,
        result: SyncResult,
    ) -> None:
        """同步单个作业到日程系统
        
        Args:
            assignment: 作业对象
            result: 同步结果对象（用于记录）
        """
        # 跳过已同步的
        if assignment.id in self.synced_assignments_ids:
            result.skipped_count += 1
            logger.debug(f"Skipping already synced assignment: {assignment.id}")
            return
        
        # 转换为日程事件格式
        event_data = self._assignment_to_event(assignment)
        
        # 创建日程事件
        # 注意：这里需要通过工具调用来创建日程事件
        # 由于这是在 Python 代码中，我们返回事件数据，由调用者决定如何创建
        event_id = await self._create_schedule_event(event_data)
        
        if event_id:
            self.synced_assignments_ids.add(assignment.id)
            result.created_count += 1
            logger.info(
                f"Synced assignment: {assignment.name} (due: {assignment.due_at})"
            )
        else:
            result.add_error(f"Failed to create event for assignment {assignment.id}")
    
    async def _sync_calendar_event(
        self,
        event: CanvasEvent,
        result: SyncResult,
    ) -> None:
        """同步单个日历事件到日程系统
        
        Args:
            event: 日历事件对象
            result: 同步结果对象
        """
        # 跳过已同步的
        if event.id in self.synced_event_ids:
            result.skipped_count += 1
            logger.debug(f"Skipping already synced event: {event.id}")
            return
        
        # 转换为日程事件格式
        event_data = self._calendar_event_to_event(event)
        
        # 创建日程事件
        event_id = await self._create_schedule_event(event_data)
        
        if event_id:
            self.synced_event_ids.add(event.id)
            result.created_count += 1
            logger.info(f"Synced event: {event.title}")
        else:
            result.add_error(f"Failed to create event for calendar event {event.id}")
    
    def _assignment_to_event(
        self,
        assignment: CanvasAssignment,
    ) -> Dict[str, Any]:
        """将作业转换为日程事件格式
        
        Args:
            assignment: 作业对象
            
        Returns:
            日程事件数据字典
        """
        # 判断优先级
        days_left = assignment.days_left
        if days_left <= 1:
            priority = "urgent"
        elif days_left <= 3:
            priority = "high"
        elif days_left <= 7:
            priority = "medium"
        else:
            priority = "low"
        
        # 提前 2 小时开始提醒
        if assignment.due_at:
            start_time = assignment.due_at - timedelta(hours=2)
            end_time = assignment.due_at
        else:
            # 如果没有截止时间，使用默认值（使用 UTC 时间）
            now = now_utc()
            start_time = now
            end_time = now + timedelta(hours=2)
        
        # 构建描述
        description_parts = [
            f"课程：{assignment.course_name}",
            f"总分：{assignment.points_possible}",
            f"状态：{self._get_workflow_state_cn(assignment.workflow_state)}",
        ]
        
        if assignment.grade:
            description_parts.append(f"成绩：{assignment.grade}")
        
        if assignment.html_url:
            description_parts.append(f"链接：{assignment.html_url}")
        
        description = "\n".join(description_parts)
        if assignment.description:
            # 移除 HTML 标签
            clean_desc = re.sub(r"<[^>]+>", "", assignment.description)
            clean_desc = clean_desc[:500]  # 限制长度
            description += f"\n\n{clean_desc}"
        
        # 构建标签
        tags = ["canvas", "作业"]
        if assignment.course_name:
            tags.append(assignment.course_name)
        if assignment.is_late:
            tags.append("已过期")
        if assignment.is_submitted:
            tags.append("已提交")
        
        return {
            "title": f"[作业] {assignment.course_name}: {assignment.name}",
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "description": description,
            "priority": priority,
            "tags": tags,
            "metadata": {
                "source": "canvas",
                "canvas_id": assignment.id,
                "course_id": assignment.course_id,
                "type": "assignment",
            },
        }
    
    def _calendar_event_to_event(
        self,
        event: CanvasEvent,
    ) -> Dict[str, Any]:
        """将日历事件转换为日程事件格式
        
        Args:
            event: 日历事件对象
            
        Returns:
            日程事件数据字典
        """
        # 判断是否是考试
        title_lower = event.title.lower()
        is_exam = (
            "exam" in title_lower
            or "考试" in title_lower
            or "quiz" in title_lower
            or "测验" in title_lower
            or "midterm" in title_lower
            or "final" in title_lower
        )
        
        # 构建标题
        if is_exam:
            title = f"[考试] {event.course_name or ''}: {event.title}"
            priority = "urgent"
        else:
            title = event.title
            priority = "medium"
        
        # 处理时间
        if event.all_day:
            # 全天事件
            if event.start_at:
                start_time = event.start_at.replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                end_time = start_time + timedelta(days=1)
            else:
                now = now_utc()
                start_time = now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                end_time = start_time + timedelta(days=1)
        else:
            if event.start_at and event.end_at:
                start_time = event.start_at
                end_time = event.end_at
            elif event.start_at:
                start_time = event.start_at
                end_time = start_time + timedelta(hours=2)
            else:
                now = now_utc()
                start_time = now
                end_time = now + timedelta(hours=2)
        
        # 构建描述
        description_parts = []
        if event.course_name:
            description_parts.append(f"课程：{event.course_name}")
        if event.description:
            import re
            clean_desc = re.sub(r"<[^>]+>", "", event.description)
            description_parts.append(clean_desc[:500])
        if event.html_url:
            description_parts.append(f"链接：{event.html_url}")
        
        description = "\n".join(description_parts) if description_parts else ""
        
        # 构建标签
        tags = ["canvas", "事件"]
        if event.course_name:
            tags.append(event.course_name)
        if is_exam:
            tags.append("考试")
        
        return {
            "title": title,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "description": description,
            "priority": priority,
            "tags": tags,
            "metadata": {
                "source": "canvas",
                "canvas_id": event.id,
                "course_id": event.course_id,
                "type": "event",
            },
        }
    
    async def _create_schedule_event(
        self,
        event_data: Dict[str, Any],
    ) -> Optional[str]:
        """创建日程事件
        
        如果提供了 event_creator 回调，则调用它创建事件。
        否则返回 None，表示需要外部处理。
        
        Args:
            event_data: 日程事件数据
            
        Returns:
            创建的事件 ID，如果失败则返回 None
        """
        if self.event_creator:
            try:
                event_id = await self.event_creator(event_data)
                if event_id:
                    logger.info(
                        f"Created schedule event: {event_data['title']}"
                    )
                return event_id
            except Exception as e:
                logger.error(f"Failed to create event: {e}")
                return None
        else:
            # 没有回调，只记录日志
            logger.debug(
                f"Event data ready (no creator): {event_data['title']}"
            )
            return None
    
    def _get_workflow_state_cn(self, state: str) -> str:
        """将提交状态转换为中文
        
        Args:
            state: 英文状态
            
        Returns:
            中文状态
        """
        mapping = {
            "submitted": "已提交",
            "graded": "已评分",
            "missing": "未提交",
            "late": "已逾期",
            "unsubmitted": "未提交",
        }
        return mapping.get(state, state)
    
    def get_pending_events(self) -> List[Dict[str, Any]]:
        """获取待创建的事件列表
        
        这个方法用于让 Agent 获取所有需要同步到日程的事件数据，
        然后由 Agent 调用日程工具来实际创建。
        
        Returns:
            待创建的事件数据列表
        """
        # 这个方法需要在实际使用时动态生成
        # 目前由 sync_to_schedule 直接处理
        return []


async def sync_canvas_to_schedule(
    client: CanvasClient,
    days_ahead: int = 60,
    include_submitted: bool = False,
    event_creator: Optional[EventCreatorCallback] = None,
) -> SyncResult:
    """便捷函数：同步 Canvas 到日程
    
    Args:
        client: Canvas API 客户端
        days_ahead: 同步未来多少天
        include_submitted: 是否包含已提交的作业
        event_creator: 日程事件创建回调函数（可选）
        
    Returns:
        同步结果
        
    Example:
        >>> # 不提供回调（只获取数据）
        >>> result = await sync_canvas_to_schedule(client)
        >>>
        >>> # 提供回调（完整同步）
        >>> async def create(event_data):
        ...     return await call_tool("add_event", event_data)
        >>> result = await sync_canvas_to_schedule(client, event_creator=create)
    """
    sync = CanvasSync(client, event_creator=event_creator)
    return await sync.sync_to_schedule(days_ahead, include_submitted)
