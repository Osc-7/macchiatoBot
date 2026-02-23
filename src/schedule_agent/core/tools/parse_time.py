"""
时间解析工具

解析自然语言时间描述，返回结构化的时间数据。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from schedule_agent.core.context.time_context import get_time_context

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult


@dataclass
class ParsedTime:
    """解析后的时间结果"""

    success: bool
    """是否解析成功"""

    start_time: Optional[datetime] = None
    """开始时间"""

    end_time: Optional[datetime] = None
    """结束时间（如果是时间段）"""

    date: Optional[date] = None
    """日期（如果只指定了日期）"""

    is_all_day: bool = False
    """是否是全天事件"""

    original_text: str = ""
    """原始文本"""

    confidence: float = 0.0
    """置信度 (0.0 - 1.0)"""

    message: str = ""
    """解析说明"""

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "success": self.success,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "date": self.date.isoformat() if self.date else None,
            "is_all_day": self.is_all_day,
            "original_text": self.original_text,
            "confidence": self.confidence,
            "message": self.message,
        }


class TimeParser:
    """
    时间解析器。

    支持解析中文自然语言时间描述，包括：
    - 相对日期：今天、明天、后天、下周、下个月等
    - 绝对日期：2月20日、2026年3月1日等
    - 时间点：3点、下午2点、晚上8点等
    - 时间段：3点到5点、上午9点-12点等
    """

    # 相对日期映射
    RELATIVE_DATES = {
        "今天": 0,
        "今日": 0,
        "明天": 1,
        "明日": 1,
        "后天": 2,
        "大后天": 3,
        "昨天": -1,
        "昨日": -1,
        "前天": -2,
    }

    # 星期映射
    WEEKDAY_MAP = {
        "周一": 0,
        "星期一": 0,
        "礼拜一": 0,
        "周二": 1,
        "星期二": 1,
        "礼拜二": 1,
        "周三": 2,
        "星期三": 2,
        "礼拜三": 2,
        "周四": 3,
        "星期四": 3,
        "礼拜四": 3,
        "周五": 4,
        "星期五": 4,
        "礼拜五": 4,
        "周六": 5,
        "星期六": 5,
        "礼拜六": 5,
        "周天": 6,
        "周日": 6,
        "星期日": 6,
        "礼拜日": 6,
    }

    # 时间段映射
    TIME_PERIODS = {
        "凌晨": (0, 5),
        "早上": (6, 9),
        "上午": (9, 12),
        "中午": (12, 14),
        "下午": (14, 18),
        "傍晚": (17, 19),
        "晚上": (18, 22),
        "夜间": (20, 23),
        "深夜": (23, 24),
    }

    def __init__(self, timezone: str = "Asia/Shanghai"):
        """
        初始化时间解析器。

        Args:
            timezone: 时区名称
        """
        self.timezone = ZoneInfo(timezone)
        self._now: Optional[datetime] = None

    @property
    def now(self) -> datetime:
        """获取当前时间"""
        if self._now is None:
            self._now = datetime.now(self.timezone)
        return self._now

    def set_now(self, now: datetime) -> None:
        """
        设置当前时间（用于测试）。

        Args:
            now: 当前时间
        """
        self._now = now

    def reset_now(self) -> None:
        """重置当前时间"""
        self._now = None

    def parse(self, text: str) -> ParsedTime:
        """
        解析时间文本。

        Args:
            text: 时间描述文本

        Returns:
            解析结果
        """
        text = text.strip()
        if not text:
            return ParsedTime(
                success=False,
                original_text=text,
                message="时间描述为空",
            )

        # 尝试解析日期和时间
        parsed_date = self._parse_date(text)
        parsed_time = self._parse_time(text)
        time_range = self._parse_time_range(text)

        # 组合结果
        if parsed_date is None:
            return ParsedTime(
                success=False,
                original_text=text,
                message="无法识别日期",
            )

        # 处理时间范围
        if time_range:
            start_dt = datetime.combine(parsed_date, time(hour=time_range[0][0], minute=time_range[0][1]))
            end_dt = datetime.combine(parsed_date, time(hour=time_range[1][0], minute=time_range[1][1]))
            start_dt = start_dt.replace(tzinfo=self.timezone)
            end_dt = end_dt.replace(tzinfo=self.timezone)

            return ParsedTime(
                success=True,
                start_time=start_dt,
                end_time=end_dt,
                date=parsed_date,
                is_all_day=False,
                original_text=text,
                confidence=0.9,
                message=f"解析为时间段: {start_dt.strftime('%Y-%m-%d %H:%M')} 至 {end_dt.strftime('%H:%M')}",
            )

        # 处理单个时间点
        if parsed_time:
            dt = datetime.combine(parsed_date, time(hour=parsed_time[0], minute=parsed_time[1]))
            dt = dt.replace(tzinfo=self.timezone)

            return ParsedTime(
                success=True,
                start_time=dt,
                date=parsed_date,
                is_all_day=False,
                original_text=text,
                confidence=0.85,
                message=f"解析为: {dt.strftime('%Y-%m-%d %H:%M')}",
            )

        # 只有日期，全天事件
        return ParsedTime(
            success=True,
            date=parsed_date,
            is_all_day=True,
            original_text=text,
            confidence=0.8,
            message=f"解析为全天: {parsed_date.strftime('%Y-%m-%d')}",
        )

    def _parse_date(self, text: str) -> Optional[date]:
        """
        解析日期部分。

        Args:
            text: 时间文本

        Returns:
            解析的日期，如果失败返回 None
        """
        today = self.now.date()

        # 1. 尝试相对日期
        for keyword, offset in self.RELATIVE_DATES.items():
            if keyword in text:
                return today + timedelta(days=offset)

        # 2. 尝试"下周X"
        next_week_match = re.search(r"下周([一二三四五六日天])", text)
        if next_week_match:
            weekday_char = next_week_match.group(1)
            weekday_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
            target_weekday = weekday_map.get(weekday_char)
            if target_weekday is not None:
                days_ahead = target_weekday - today.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                return today + timedelta(days=days_ahead)

        # 3. 尝试"这周X"或"本周X"
        this_week_match = re.search(r"[这本]周([一二三四五六日天])", text)
        if this_week_match:
            weekday_char = this_week_match.group(1)
            weekday_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
            target_weekday = weekday_map.get(weekday_char)
            if target_weekday is not None:
                days_ahead = target_weekday - today.weekday()
                if days_ahead < 0:
                    days_ahead += 7
                return today + timedelta(days=days_ahead)

        # 4. 尝试星期几
        for keyword, weekday in self.WEEKDAY_MAP.items():
            if keyword in text:
                days_ahead = weekday - today.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                return today + timedelta(days=days_ahead)

        # 5. 尝试"X天后"
        days_later_match = re.search(r"(\d+)\s*天[以]?后", text)
        if days_later_match:
            days = int(days_later_match.group(1))
            return today + timedelta(days=days)

        # 6. 尝试"X月X日"格式
        date_match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", text)
        if date_match:
            month = int(date_match.group(1))
            day = int(date_match.group(2))
            try:
                # 默认使用当前年份
                result = date(today.year, month, day)
                # 如果日期已过，使用下一年
                if result < today:
                    result = date(today.year + 1, month, day)
                return result
            except ValueError:
                pass

        # 7. 尝试"YYYY年X月X日"格式
        full_date_match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", text)
        if full_date_match:
            year = int(full_date_match.group(1))
            month = int(full_date_match.group(2))
            day = int(full_date_match.group(3))
            try:
                return date(year, month, day)
            except ValueError:
                pass

        # 8. 尝试"X周后"
        weeks_later_match = re.search(r"(\d+)\s*周[以]?后", text)
        if weeks_later_match:
            weeks = int(weeks_later_match.group(1))
            return today + timedelta(weeks=weeks)

        # 9. 尝试"下个月X日"
        next_month_match = re.search(r"下个?月\s*(\d{1,2})\s*[日号]?", text)
        if next_month_match:
            day = int(next_month_match.group(1))
            if today.month == 12:
                next_month = date(today.year + 1, 1, 1)
            else:
                next_month = date(today.year, today.month + 1, 1)
            try:
                return date(next_month.year, next_month.month, day)
            except ValueError:
                pass

        # 10. 如果只包含"下个月"没有具体日期
        if "下个月" in text or "下月" in text:
            if today.month == 12:
                return date(today.year + 1, 1, 1)
            else:
                return date(today.year, today.month + 1, 1)

        return None

    def _parse_time(self, text: str) -> Optional[Tuple[int, int]]:
        """
        解析时间部分。

        Args:
            text: 时间文本

        Returns:
            解析的时间元组 (hour, minute)，如果失败返回 None
        """
        # 1. 尝试"X点X分"格式
        time_match = re.search(r"(\d{1,2})\s*[点时:：]\s*(\d{1,2})\s*[分]?", text)
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return (hour, minute)

        # 2. 尝试"X点"格式
        hour_match = re.search(r"(\d{1,2})\s*[点时](?!\d)", text)
        if hour_match:
            hour = int(hour_match.group(1))
            # 处理时间段前缀
            for period, (start, end) in self.TIME_PERIODS.items():
                if period in text:
                    # 如果是下午且小时数小于12，加12
                    if period in ("下午", "晚上", "傍晚", "夜间", "深夜") and hour < 12:
                        hour += 12
                    elif period in ("凌晨", "早上", "上午") and hour == 12:
                        hour = 0
                    break

            if 0 <= hour <= 23:
                return (hour, 0)

        # 3. 尝试时间段
        for period, (start, end) in self.TIME_PERIODS.items():
            if period in text:
                # 返回时间段的开始时间
                return (start, 0)

        return None

    def _parse_time_range(self, text: str) -> Optional[Tuple[Tuple[int, int], Tuple[int, int]]]:
        """
        解析时间范围。

        Args:
            text: 时间文本

        Returns:
            解析的时间范围 ((start_hour, start_min), (end_hour, end_min))，
            如果失败返回 None
        """
        # 尝试"X点到Y点"或"X点-Y点"格式
        range_match = re.search(
            r"(\d{1,2})\s*[点时]?\s*[到至\-~]+\s*(\d{1,2})\s*[点时]?",
            text,
        )
        if range_match:
            start_hour = int(range_match.group(1))
            end_hour = int(range_match.group(2))

            # 处理时间段前缀
            for period, (start, end) in self.TIME_PERIODS.items():
                if period in text:
                    if period in ("下午", "晚上", "傍晚", "夜间", "深夜"):
                        if start_hour < 12:
                            start_hour += 12
                        if end_hour < 12:
                            end_hour += 12
                    break

            if 0 <= start_hour <= 23 and 0 <= end_hour <= 23:
                return ((start_hour, 0), (end_hour, 0))

        return None


class ParseTimeTool(BaseTool):
    """时间解析工具"""

    def __init__(self, timezone: str = "Asia/Shanghai"):
        """
        初始化时间解析工具。

        Args:
            timezone: 时区名称
        """
        self._parser = TimeParser(timezone=timezone)

    @property
    def name(self) -> str:
        return "parse_time"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="parse_time",
            description="""解析自然语言时间描述，返回结构化的时间数据。

这个工具用于解析用户输入的时间描述，支持：
- 相对日期：今天、明天、后天、下周、下个月等
- 绝对日期：2月20日、2026年3月1日等
- 时间点：3点、下午2点、晚上8点等
- 时间段：3点到5点、上午9点-12点等

返回的结果包含：
- start_time: 开始时间（ISO 格式）
- end_time: 结束时间（如果是时间段）
- date: 日期
- is_all_day: 是否是全天事件
- confidence: 解析置信度""",
            parameters=[
                ToolParameter(
                    name="time_text",
                    type="string",
                    description="时间描述文本，如'明天下午3点'、'下周一下午2点到4点'",
                    required=True,
                ),
            ],
            examples=[
                {
                    "description": "解析相对日期",
                    "params": {"time_text": "明天下午3点"},
                },
                {
                    "description": "解析时间段",
                    "params": {"time_text": "下周一上午9点到11点"},
                },
                {
                    "description": "解析绝对日期",
                    "params": {"time_text": "3月15日"},
                },
                {
                    "description": "解析带年份的日期",
                    "params": {"time_text": "2026年5月1日下午2点"},
                },
            ],
            usage_notes=[
                "支持中文自然语言时间描述",
                "如果没有指定时间，默认为全天事件",
                "置信度越高表示解析越可靠",
            ],
            tags=['时间', '解析'],
        )

    async def execute(self, **kwargs) -> ToolResult:
        """
        执行时间解析。

        Args:
            time_text: 时间描述文本

        Returns:
            解析结果
        """
        time_text = kwargs.get("time_text", "")
        if not time_text:
            return ToolResult(
                success=False,
                error="MISSING_TIME_TEXT",
                message="缺少时间描述参数",
            )

        result = self._parser.parse(time_text)

        return ToolResult(
            success=result.success,
            data=result.to_dict(),
            message=result.message,
            metadata={
                "original_text": result.original_text,
                "confidence": result.confidence,
            },
        )

    def set_now(self, now: datetime) -> None:
        """
        设置当前时间（用于测试）。

        Args:
            now: 当前时间
        """
        self._parser.set_now(now)

    def reset_now(self) -> None:
        """重置当前时间"""
        self._parser.reset_now()
