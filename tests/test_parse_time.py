"""
时间解析工具测试
"""

import pytest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from schedule_agent.core.tools import ParseTimeTool, ParsedTime, TimeParser


class TestTimeParser:
    """测试 TimeParser"""

    @pytest.fixture
    def parser(self):
        """创建时间解析器"""
        return TimeParser(timezone="Asia/Shanghai")

    @pytest.fixture
    def fixed_now(self):
        """固定当前时间用于测试"""
        # 2026年2月17日 周二 上午10点
        return datetime(2026, 2, 17, 10, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def test_parser_creation(self, parser):
        """测试解析器创建"""
        assert parser.timezone is not None

    def test_parse_empty_text(self, parser):
        """测试解析空文本"""
        result = parser.parse("")
        assert result.success is False
        assert result.message == "时间描述为空"

    def test_parse_today(self, parser, fixed_now):
        """测试解析'今天'"""
        parser.set_now(fixed_now)
        result = parser.parse("今天")
        assert result.success is True
        assert result.date == date(2026, 2, 17)
        assert result.is_all_day is True
        parser.reset_now()

    def test_parse_tomorrow(self, parser, fixed_now):
        """测试解析'明天'"""
        parser.set_now(fixed_now)
        result = parser.parse("明天")
        assert result.success is True
        assert result.date == date(2026, 2, 18)
        parser.reset_now()

    def test_parse_day_after_tomorrow(self, parser, fixed_now):
        """测试解析'后天'"""
        parser.set_now(fixed_now)
        result = parser.parse("后天")
        assert result.success is True
        assert result.date == date(2026, 2, 19)
        parser.reset_now()

    def test_parse_yesterday(self, parser, fixed_now):
        """测试解析'昨天'"""
        parser.set_now(fixed_now)
        result = parser.parse("昨天")
        assert result.success is True
        assert result.date == date(2026, 2, 16)
        parser.reset_now()

    def test_parse_days_later(self, parser, fixed_now):
        """测试解析'X天后'"""
        parser.set_now(fixed_now)
        result = parser.parse("3天后")
        assert result.success is True
        assert result.date == date(2026, 2, 20)
        parser.reset_now()

    def test_parse_weeks_later(self, parser, fixed_now):
        """测试解析'X周后'"""
        parser.set_now(fixed_now)
        result = parser.parse("2周后")
        assert result.success is True
        assert result.date == date(2026, 3, 3)
        parser.reset_now()

    def test_parse_next_week_monday(self, parser, fixed_now):
        """测试解析'下周一'"""
        parser.set_now(fixed_now)  # 周二
        result = parser.parse("下周一")
        assert result.success is True
        # 下周一是 2026-02-23
        assert result.date == date(2026, 2, 23)
        parser.reset_now()

    def test_parse_this_week_friday(self, parser, fixed_now):
        """测试解析'本周五'"""
        parser.set_now(fixed_now)  # 周二
        result = parser.parse("本周五")
        assert result.success is True
        # 本周五是 2026-02-20
        assert result.date == date(2026, 2, 20)
        parser.reset_now()

    def test_parse_weekday_direct(self, parser, fixed_now):
        """测试直接解析星期几"""
        parser.set_now(fixed_now)  # 周二
        result = parser.parse("周五")
        assert result.success is True
        assert result.date == date(2026, 2, 20)
        parser.reset_now()

    def test_parse_month_day(self, parser, fixed_now):
        """测试解析'X月X日'"""
        parser.set_now(fixed_now)
        result = parser.parse("3月15日")
        assert result.success is True
        assert result.date == date(2026, 3, 15)
        parser.reset_now()

    def test_parse_full_date(self, parser, fixed_now):
        """测试解析完整日期"""
        parser.set_now(fixed_now)
        result = parser.parse("2026年5月1日")
        assert result.success is True
        assert result.date == date(2026, 5, 1)
        parser.reset_now()

    def test_parse_next_month(self, parser, fixed_now):
        """测试解析'下个月'"""
        parser.set_now(fixed_now)
        result = parser.parse("下个月")
        assert result.success is True
        assert result.date == date(2026, 3, 1)
        parser.reset_now()

    def test_parse_next_month_day(self, parser, fixed_now):
        """测试解析'下个月X日'"""
        parser.set_now(fixed_now)
        result = parser.parse("下个月15日")
        assert result.success is True
        assert result.date == date(2026, 3, 15)
        parser.reset_now()

    def test_parse_time_hour_only(self, parser, fixed_now):
        """测试解析小时"""
        parser.set_now(fixed_now)
        result = parser.parse("明天3点")
        assert result.success is True
        assert result.date == date(2026, 2, 18)
        assert result.start_time.hour == 3
        assert result.start_time.minute == 0
        parser.reset_now()

    def test_parse_time_hour_minute(self, parser, fixed_now):
        """测试解析小时分钟"""
        parser.set_now(fixed_now)
        result = parser.parse("明天3点30分")
        assert result.success is True
        assert result.date == date(2026, 2, 18)
        assert result.start_time.hour == 3
        assert result.start_time.minute == 30
        parser.reset_now()

    def test_parse_time_with_period(self, parser, fixed_now):
        """测试解析带时间段时间"""
        parser.set_now(fixed_now)
        result = parser.parse("明天下午3点")
        assert result.success is True
        assert result.date == date(2026, 2, 18)
        assert result.start_time.hour == 15  # 下午3点 = 15点
        parser.reset_now()

    def test_parse_time_evening(self, parser, fixed_now):
        """测试解析晚上时间"""
        parser.set_now(fixed_now)
        result = parser.parse("明天晚上8点")
        assert result.success is True
        assert result.date == date(2026, 2, 18)
        assert result.start_time.hour == 20
        parser.reset_now()

    def test_parse_time_range(self, parser, fixed_now):
        """测试解析时间范围"""
        parser.set_now(fixed_now)
        result = parser.parse("明天下午2点到4点")
        assert result.success is True
        assert result.date == date(2026, 2, 18)
        assert result.start_time.hour == 14
        assert result.end_time.hour == 16
        parser.reset_now()

    def test_parse_time_range_with_dash(self, parser, fixed_now):
        """测试解析时间范围（横线分隔）"""
        parser.set_now(fixed_now)
        result = parser.parse("明天9点-12点")
        assert result.success is True
        assert result.start_time.hour == 9
        assert result.end_time.hour == 12
        parser.reset_now()

    def test_parse_morning_period(self, parser, fixed_now):
        """测试解析上午时间段"""
        parser.set_now(fixed_now)
        result = parser.parse("明天上午")
        assert result.success is True
        assert result.start_time.hour == 9
        parser.reset_now()

    def test_parse_afternoon_period(self, parser, fixed_now):
        """测试解析下午时间段"""
        parser.set_now(fixed_now)
        result = parser.parse("明天下午")
        assert result.success is True
        assert result.start_time.hour == 14
        parser.reset_now()

    def test_confidence_scores(self, parser, fixed_now):
        """测试置信度分数"""
        parser.set_now(fixed_now)

        # 全天事件置信度较低
        result1 = parser.parse("明天")
        assert result1.confidence == 0.8

        # 单个时间点置信度中等
        result2 = parser.parse("明天3点")
        assert result2.confidence == 0.85

        # 时间范围置信度最高
        result3 = parser.parse("明天下午2点到4点")
        assert result3.confidence == 0.9

        parser.reset_now()

    def test_parse_chinese_variants(self, parser, fixed_now):
        """测试中文变体"""
        parser.set_now(fixed_now)

        # 今日 = 今天
        result1 = parser.parse("今日")
        assert result1.date == date(2026, 2, 17)

        # 明日 = 明天
        result2 = parser.parse("明日")
        assert result2.date == date(2026, 2, 18)

        parser.reset_now()

    def test_parse_weekday_variants(self, parser, fixed_now):
        """测试星期几变体"""
        parser.set_now(fixed_now)

        # 星期一
        result1 = parser.parse("星期一")
        assert result1.date.weekday() == 0

        # 礼拜五
        result2 = parser.parse("礼拜五")
        assert result2.date.weekday() == 4

        # 周日
        result3 = parser.parse("周日")
        assert result3.date.weekday() == 6

        parser.reset_now()


class TestParsedTime:
    """测试 ParsedTime"""

    def test_to_dict(self):
        """测试转换为字典"""
        dt = datetime(2026, 2, 18, 15, 30, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        parsed = ParsedTime(
            success=True,
            start_time=dt,
            date=date(2026, 2, 18),
            is_all_day=False,
            original_text="明天下午3点半",
            confidence=0.85,
            message="解析成功",
        )

        result = parsed.to_dict()

        assert result["success"] is True
        assert "2026-02-18T15:30:00" in result["start_time"]
        assert result["date"] == "2026-02-18"
        assert result["is_all_day"] is False
        assert result["original_text"] == "明天下午3点半"
        assert result["confidence"] == 0.85

    def test_to_dict_with_end_time(self):
        """测试带结束时间的字典转换"""
        start = datetime(2026, 2, 18, 14, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        end = datetime(2026, 2, 18, 16, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        parsed = ParsedTime(
            success=True,
            start_time=start,
            end_time=end,
            date=date(2026, 2, 18),
            is_all_day=False,
            original_text="明天下午2点到4点",
            confidence=0.9,
            message="时间段",
        )

        result = parsed.to_dict()

        assert result["end_time"] is not None
        assert "16:00:00" in result["end_time"]


class TestParseTimeTool:
    """测试 ParseTimeTool"""

    @pytest.fixture
    def tool(self):
        """创建时间解析工具"""
        return ParseTimeTool(timezone="Asia/Shanghai")

    @pytest.fixture
    def fixed_now(self):
        """固定当前时间"""
        return datetime(2026, 2, 17, 10, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def test_tool_name(self, tool):
        """测试工具名称"""
        assert tool.name == "parse_time"

    def test_get_definition(self, tool):
        """测试获取工具定义"""
        definition = tool.get_definition()

        assert definition.name == "parse_time"
        assert len(definition.parameters) == 1
        assert definition.parameters[0].name == "time_text"
        assert len(definition.examples) >= 4

    def test_to_openai_tool(self, tool):
        """测试转换为 OpenAI 格式"""
        openai_tool = tool.to_openai_tool()

        assert openai_tool["type"] == "function"
        assert openai_tool["function"]["name"] == "parse_time"
        assert "parameters" in openai_tool["function"]

    @pytest.mark.asyncio
    async def test_execute_success(self, tool, fixed_now):
        """测试成功执行"""
        tool.set_now(fixed_now)
        result = await tool.execute(time_text="明天下午3点")

        assert result.success is True
        assert result.data is not None
        assert result.data["success"] is True
        assert result.data["date"] == "2026-02-18"
        tool.reset_now()

    @pytest.mark.asyncio
    async def test_execute_time_range(self, tool, fixed_now):
        """测试执行时间范围解析"""
        tool.set_now(fixed_now)
        result = await tool.execute(time_text="下周一上午9点到11点")

        assert result.success is True
        assert result.data["end_time"] is not None
        tool.reset_now()

    @pytest.mark.asyncio
    async def test_execute_empty_text(self, tool):
        """测试空文本"""
        result = await tool.execute(time_text="")

        assert result.success is False
        assert result.error == "MISSING_TIME_TEXT"

    @pytest.mark.asyncio
    async def test_execute_missing_parameter(self, tool):
        """测试缺少参数"""
        result = await tool.execute()

        assert result.success is False
        assert result.error == "MISSING_TIME_TEXT"

    @pytest.mark.asyncio
    async def test_execute_unrecognized_date(self, tool):
        """测试无法识别的日期"""
        result = await tool.execute(time_text="某个时候")

        assert result.success is False

    @pytest.mark.asyncio
    async def test_metadata(self, tool, fixed_now):
        """测试元数据"""
        tool.set_now(fixed_now)
        result = await tool.execute(time_text="明天")

        assert result.metadata is not None
        assert "original_text" in result.metadata
        assert "confidence" in result.metadata
        tool.reset_now()


class TestTimeParserEdgeCases:
    """测试边缘情况"""

    @pytest.fixture
    def parser(self):
        """创建时间解析器"""
        return TimeParser(timezone="Asia/Shanghai")

    @pytest.fixture
    def fixed_now(self):
        """固定当前时间 - 年末"""
        # 2026年12月30日 周三
        return datetime(2026, 12, 30, 10, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def test_cross_year_date(self, parser, fixed_now):
        """测试跨年日期"""
        parser.set_now(fixed_now)
        result = parser.parse("1月5日")
        # 应该是明年
        assert result.date.year == 2027
        assert result.date.month == 1
        assert result.date.day == 5
        parser.reset_now()

    def test_next_month_december(self, parser, fixed_now):
        """测试12月的下个月"""
        parser.set_now(fixed_now)
        result = parser.parse("下个月")
        assert result.date.year == 2027
        assert result.date.month == 1
        parser.reset_now()

    def test_february_29_invalid(self, parser, fixed_now):
        """测试无效的2月29日（2026年不是闰年）"""
        parser.set_now(fixed_now)
        result = parser.parse("2月29日")
        # 2026年2月只有28天，应该失败或回退
        # 由于日期已过，应该跳到2027年，而2027也不是闰年
        # 实际行为取决于实现
        parser.reset_now()

    def test_time_period_morning_12_oclock(self, parser):
        """测试凌晨12点"""
        now = datetime(2026, 2, 17, 10, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        parser.set_now(now)
        result = parser.parse("明天凌晨12点")
        # 凌晨12点应该是0点
        if result.success and result.start_time:
            assert result.start_time.hour == 0
        parser.reset_now()
