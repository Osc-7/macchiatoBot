"""
时间上下文管理

提供当前时间上下文信息，用于 LLM 系统提示注入。
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo


@dataclass
class TimeContext:
    """
    时间上下文。

    包含当前时间的各种表示形式，用于 LLM 系统提示注入。
    """

    now: datetime
    """当前时间（带时区）"""

    today: date
    """今天的日期"""

    timezone_name: str
    """时区名称"""

    @property
    def weekday_cn(self) -> str:
        """中文星期几"""
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        return weekdays[self.today.weekday()]

    @property
    def time_period(self) -> str:
        """当前时间段描述"""
        hour = self.now.hour
        if 5 <= hour < 9:
            return "早上"
        elif 9 <= hour < 12:
            return "上午"
        elif 12 <= hour < 14:
            return "中午"
        elif 14 <= hour < 18:
            return "下午"
        elif 18 <= hour < 22:
            return "晚上"
        else:
            return "深夜"

    @property
    def is_weekend(self) -> bool:
        """是否是周末"""
        return self.today.weekday() >= 5

    @property
    def start_of_week(self) -> date:
        """本周开始日期（周一）"""
        return self.today - timedelta(days=self.today.weekday())

    @property
    def end_of_week(self) -> date:
        """本周结束日期（周日）"""
        return self.start_of_week + timedelta(days=6)

    @property
    def start_of_month(self) -> date:
        """本月开始日期"""
        return self.today.replace(day=1)

    @property
    def end_of_month(self) -> date:
        """本月结束日期"""
        if self.today.month == 12:
            next_month = self.today.replace(year=self.today.year + 1, month=1, day=1)
        else:
            next_month = self.today.replace(month=self.today.month + 1, day=1)
        return next_month - timedelta(days=1)

    def to_prompt_string(self) -> str:
        """
        生成用于 LLM 系统提示的时间上下文字符串。

        Returns:
            格式化的时间上下文字符串
        """
        lines = [
            f"当前时间: {self.now.strftime('%Y年%m月%d日 %H:%M:%S')}",
            f"日期: {self.today.strftime('%Y-%m-%d')} (星期{self.weekday_cn})",
            f"时间段: {self.time_period}",
            f"时区: {self.timezone_name}",
            f"是否周末: {'是' if self.is_weekend else '否'}",
            f"本周: {self.start_of_week.strftime('%Y-%m-%d')} 至 {self.end_of_week.strftime('%Y-%m-%d')}",
            f"本月: {self.start_of_month.strftime('%Y-%m-%d')} 至 {self.end_of_month.strftime('%Y-%m-%d')}",
        ]
        return "\n".join(lines)


# 与每条用户消息前缀一致：供模型将「发送时刻」与会话流对齐（通常为分钟粒度，降低无谓抖动）。
USER_MESSAGE_TIME_LINE_FMT = "%Y-%m-%d %H:%M"


def format_user_message_time_prefix(timezone: Optional[str] = None) -> str:
    """
    本轮用户消息正文前应附加的前缀（含尾部空格），形如 ``[Time:2026-04-29 14:30] ``。
    实时时钟不在 system prompt 中注入，以利前缀缓存；权威时间在用户消息前缀。
    """
    tc = get_time_context(timezone)
    line = tc.now.strftime(USER_MESSAGE_TIME_LINE_FMT)
    return f"[Time:{line}] "


def apply_user_message_time_prefix(body: str, timezone: Optional[str] = None) -> str:
    """
    在用户消息正文前附加发送时刻前缀；若正文已以 ``[Time:`` 开头则不再重复。
    """
    raw = body if body is not None else ""
    if raw.lstrip().startswith("[Time:"):
        return raw
    pre = format_user_message_time_prefix(timezone)
    return pre + raw


def get_time_context(timezone: Optional[str] = None) -> TimeContext:
    """
    获取当前时间上下文。

    Args:
        timezone: 时区名称（如 "Asia/Shanghai"），默认使用 "Asia/Shanghai"

    Returns:
        TimeContext 实例
    """
    tz_name = timezone or "Asia/Shanghai"
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    return TimeContext(
        now=now,
        today=now.date(),
        timezone_name=tz_name,
    )


def get_relative_date_desc(days_offset: int, base_date: Optional[date] = None) -> str:
    """
    获取相对日期的中文描述。

    Args:
        days_offset: 天数偏移量（正数为未来，负数为过去）
        base_date: 基准日期，默认为今天

    Returns:
        相对日期的中文描述
    """
    if base_date is None:
        base_date = date.today()

    target_date = base_date + timedelta(days=days_offset)

    if days_offset == 0:
        return "今天"
    elif days_offset == 1:
        return "明天"
    elif days_offset == 2:
        return "后天"
    elif days_offset == -1:
        return "昨天"
    elif days_offset == -2:
        return "前天"
    elif days_offset > 0:
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"{target_date.strftime('%m月%d日')}（{weekday_names[target_date.weekday()]}）"
    else:
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"{target_date.strftime('%m月%d日')}（{weekday_names[target_date.weekday()]}）"
