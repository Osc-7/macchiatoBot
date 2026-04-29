"""
上下文管理测试
"""

from datetime import datetime, date
from zoneinfo import ZoneInfo

from agent_core.context import (
    ConversationContext,
    TimeContext,
    apply_user_message_time_prefix,
    get_time_context,
    get_relative_date_desc,
)


class TestTimeContext:
    """测试 TimeContext"""

    def test_time_context_creation(self):
        """测试时间上下文创建"""
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 2, 17, 14, 30, 0, tzinfo=tz)

        ctx = TimeContext(
            now=now,
            today=now.date(),
            timezone_name="Asia/Shanghai",
        )

        assert ctx.timezone_name == "Asia/Shanghai"
        assert ctx.today == date(2026, 2, 17)

    def test_weekday_cn(self):
        """测试中文星期几"""
        # 2026-02-17 是周二
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 2, 17, 14, 0, 0, tzinfo=tz)

        ctx = TimeContext(now=now, today=now.date(), timezone_name="Asia/Shanghai")

        assert ctx.weekday_cn == "二"

        # 测试周日
        sunday = datetime(2026, 2, 22, 14, 0, 0, tzinfo=tz)
        ctx_sunday = TimeContext(
            now=sunday, today=sunday.date(), timezone_name="Asia/Shanghai"
        )
        assert ctx_sunday.weekday_cn == "日"

    def test_time_period(self):
        """测试时间段描述"""
        tz = ZoneInfo("Asia/Shanghai")

        # 早上
        morning = datetime(2026, 2, 17, 8, 0, 0, tzinfo=tz)
        ctx = TimeContext(
            now=morning, today=morning.date(), timezone_name="Asia/Shanghai"
        )
        assert ctx.time_period == "早上"

        # 上午
        am = datetime(2026, 2, 17, 10, 0, 0, tzinfo=tz)
        ctx = TimeContext(now=am, today=am.date(), timezone_name="Asia/Shanghai")
        assert ctx.time_period == "上午"

        # 中午
        noon = datetime(2026, 2, 17, 12, 0, 0, tzinfo=tz)
        ctx = TimeContext(now=noon, today=noon.date(), timezone_name="Asia/Shanghai")
        assert ctx.time_period == "中午"

        # 下午
        pm = datetime(2026, 2, 17, 15, 0, 0, tzinfo=tz)
        ctx = TimeContext(now=pm, today=pm.date(), timezone_name="Asia/Shanghai")
        assert ctx.time_period == "下午"

        # 晚上
        evening = datetime(2026, 2, 17, 20, 0, 0, tzinfo=tz)
        ctx = TimeContext(
            now=evening, today=evening.date(), timezone_name="Asia/Shanghai"
        )
        assert ctx.time_period == "晚上"

        # 深夜
        night = datetime(2026, 2, 17, 23, 0, 0, tzinfo=tz)
        ctx = TimeContext(now=night, today=night.date(), timezone_name="Asia/Shanghai")
        assert ctx.time_period == "深夜"

    def test_is_weekend(self):
        """测试是否周末"""
        tz = ZoneInfo("Asia/Shanghai")

        # 周二
        tuesday = datetime(2026, 2, 17, 14, 0, 0, tzinfo=tz)
        ctx = TimeContext(
            now=tuesday, today=tuesday.date(), timezone_name="Asia/Shanghai"
        )
        assert ctx.is_weekend is False

        # 周六
        saturday = datetime(2026, 2, 21, 14, 0, 0, tzinfo=tz)
        ctx = TimeContext(
            now=saturday, today=saturday.date(), timezone_name="Asia/Shanghai"
        )
        assert ctx.is_weekend is True

        # 周日
        sunday = datetime(2026, 2, 22, 14, 0, 0, tzinfo=tz)
        ctx = TimeContext(
            now=sunday, today=sunday.date(), timezone_name="Asia/Shanghai"
        )
        assert ctx.is_weekend is True

    def test_start_of_week(self):
        """测试本周开始日期"""
        tz = ZoneInfo("Asia/Shanghai")
        # 2026-02-17 是周二，本周开始应该是 2026-02-16（周一）
        now = datetime(2026, 2, 17, 14, 0, 0, tzinfo=tz)
        ctx = TimeContext(now=now, today=now.date(), timezone_name="Asia/Shanghai")

        assert ctx.start_of_week == date(2026, 2, 16)

    def test_end_of_week(self):
        """测试本周结束日期"""
        tz = ZoneInfo("Asia/Shanghai")
        # 2026-02-17 是周二，本周结束应该是 2026-02-22（周日）
        now = datetime(2026, 2, 17, 14, 0, 0, tzinfo=tz)
        ctx = TimeContext(now=now, today=now.date(), timezone_name="Asia/Shanghai")

        assert ctx.end_of_week == date(2026, 2, 22)

    def test_start_of_month(self):
        """测试本月开始日期"""
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 2, 17, 14, 0, 0, tzinfo=tz)
        ctx = TimeContext(now=now, today=now.date(), timezone_name="Asia/Shanghai")

        assert ctx.start_of_month == date(2026, 2, 1)

    def test_end_of_month(self):
        """测试本月结束日期"""
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 2, 17, 14, 0, 0, tzinfo=tz)
        ctx = TimeContext(now=now, today=now.date(), timezone_name="Asia/Shanghai")

        assert ctx.end_of_month == date(2026, 2, 28)

        # 测试12月
        dec = datetime(2026, 12, 15, 14, 0, 0, tzinfo=tz)
        ctx_dec = TimeContext(now=dec, today=dec.date(), timezone_name="Asia/Shanghai")
        assert ctx_dec.end_of_month == date(2026, 12, 31)

    def test_to_prompt_string(self):
        """测试生成提示字符串"""
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 2, 17, 14, 30, 0, tzinfo=tz)
        ctx = TimeContext(now=now, today=now.date(), timezone_name="Asia/Shanghai")

        prompt = ctx.to_prompt_string()

        assert "2026年02月17日" in prompt
        assert "星期二" in prompt
        assert "Asia/Shanghai" in prompt
        assert "下午" in prompt


class TestGetTimeContext:
    """测试 get_time_context 函数"""

    def test_default_timezone(self):
        """测试默认时区"""
        ctx = get_time_context()

        assert ctx.timezone_name == "Asia/Shanghai"
        assert ctx.now.tzinfo is not None

    def test_custom_timezone(self):
        """测试自定义时区"""
        ctx = get_time_context("America/New_York")

        assert ctx.timezone_name == "America/New_York"


class TestApplyUserMessageTimePrefix:
    def test_prefix_then_body(self):
        out = apply_user_message_time_prefix("你好", "Asia/Shanghai")
        assert out.startswith("[Time:")
        assert "你好" in out

    def test_skips_when_already_prefixed(self):
        raw = "[Time:2099-01-01 12:00] 已有"
        assert apply_user_message_time_prefix(raw, "Asia/Shanghai") == raw


class TestGetRelativeDateDesc:
    """测试 get_relative_date_desc 函数"""

    def test_today(self):
        """测试今天"""
        base = date(2026, 2, 17)
        assert get_relative_date_desc(0, base) == "今天"

    def test_tomorrow(self):
        """测试明天"""
        base = date(2026, 2, 17)
        assert get_relative_date_desc(1, base) == "明天"

    def test_day_after_tomorrow(self):
        """测试后天"""
        base = date(2026, 2, 17)
        assert get_relative_date_desc(2, base) == "后天"

    def test_yesterday(self):
        """测试昨天"""
        base = date(2026, 2, 17)
        assert get_relative_date_desc(-1, base) == "昨天"

    def test_day_before_yesterday(self):
        """测试前天"""
        base = date(2026, 2, 17)
        assert get_relative_date_desc(-2, base) == "前天"

    def test_future_date(self):
        """测试未来日期"""
        base = date(2026, 2, 17)
        # 7天后
        result = get_relative_date_desc(7, base)
        assert "02月24日" in result
        assert "周二" in result


class TestConversationContext:
    """测试 ConversationContext"""

    def test_empty_context(self):
        """测试空上下文"""
        ctx = ConversationContext()

        assert len(ctx) == 0
        assert ctx.get_messages() == []

    def test_add_user_message(self):
        """测试添加用户消息"""
        ctx = ConversationContext()
        ctx.add_user_message("你好")

        messages = ctx.get_messages()
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "你好"

    def test_add_assistant_message(self):
        """测试添加助手消息"""
        ctx = ConversationContext()
        ctx.add_assistant_message("你好！有什么可以帮助你的？")

        messages = ctx.get_messages()
        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert messages[0]["content"] == "你好！有什么可以帮助你的？"

    def test_add_assistant_message_with_tools(self):
        """测试添加带工具调用的助手消息"""
        ctx = ConversationContext()
        ctx.add_assistant_message(
            content=None,
            tool_calls=[
                {
                    "id": "call_123",
                    "function": {"name": "create_event", "arguments": "{}"},
                }
            ],
        )

        messages = ctx.get_messages()
        assert len(messages) == 1
        assert "tool_calls" in messages[0]

    def test_add_tool_result(self):
        """测试添加工具结果"""
        ctx = ConversationContext()
        ctx.add_tool_result(
            tool_call_id="call_123",
            result={"success": True, "message": "创建成功"},
        )

        messages = ctx.get_messages()
        assert len(messages) == 1
        assert messages[0]["role"] == "tool"
        assert messages[0]["tool_call_id"] == "call_123"

    def test_add_tool_result_with_string(self):
        """测试添加字符串工具结果"""
        ctx = ConversationContext()
        ctx.add_tool_result(tool_call_id="call_123", result="操作成功")

        messages = ctx.get_messages()
        assert messages[0]["content"] == "操作成功"

    def test_conversation_flow(self):
        """测试完整对话流程"""
        ctx = ConversationContext()

        # 用户提问
        ctx.add_user_message("帮我创建一个会议")

        # 助手调用工具
        ctx.add_assistant_message(
            content=None,
            tool_calls=[
                {
                    "id": "call_123",
                    "function": {"name": "create_event", "arguments": "{}"},
                }
            ],
        )

        # 工具返回结果
        ctx.add_tool_result(tool_call_id="call_123", result={"success": True})

        # 助手回复
        ctx.add_assistant_message("已成功创建会议")

        messages = ctx.get_messages()
        assert len(messages) == 4
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "tool"
        assert messages[3]["role"] == "assistant"

    def test_clear(self):
        """测试清空上下文"""
        ctx = ConversationContext()
        ctx.add_user_message("你好")
        ctx.add_assistant_message("你好！")

        assert len(ctx) == 2

        ctx.clear()

        assert len(ctx) == 0

    def test_no_message_count_cap(self):
        """不按条数裁剪；上下文由 token 与 Kernel 压缩控制。"""
        ctx = ConversationContext()
        n = 120
        for i in range(n):
            ctx.add_user_message(f"消息 {i}")
        assert len(ctx) == n
        assert "消息 0" in ctx.get_messages()[0]["content"]
        assert "消息 119" in ctx.get_messages()[-1]["content"]
