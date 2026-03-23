"""
记忆系统测试

覆盖四层记忆架构的核心功能：
- 工作记忆 token 估算与窗口检测
- 短期记忆 FIFO 队列
- 长期记忆条目管理
- 内容记忆存储与检索
- 记忆检索策略
"""

import tempfile
from pathlib import Path


from agent_core.memory.types import MemoryEntry, SessionSummary
from agent_core.memory.working_memory import (
    WorkingMemory,
    estimate_messages_tokens,
    estimate_tokens,
)
from agent_core.memory.short_term import ShortTermMemory
from agent_core.memory.long_term import LongTermMemory
from agent_core.memory.content_memory import ContentMemory
from agent_core.memory.recall import RecallPolicy, RecallResult
from agent_core.context.conversation import ConversationContext


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_english_text(self):
        tokens = estimate_tokens("Hello world, this is a test")
        assert tokens > 0

    def test_chinese_text(self):
        tokens = estimate_tokens("你好世界，这是一个测试")
        assert tokens > 0

    def test_mixed_text(self):
        tokens = estimate_tokens("Hello 你好 world 世界")
        assert tokens > 0

    def test_messages_tokens(self):
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮你的？"},
        ]
        tokens = estimate_messages_tokens(messages)
        assert tokens > 0


class TestWorkingMemory:
    def test_init(self):
        ctx = ConversationContext()
        wm = WorkingMemory(ctx, max_tokens=1000)
        assert wm.context is ctx
        assert wm.running_summary is None

    def test_should_compress_below(self):
        ctx = ConversationContext()
        wm = WorkingMemory(ctx, max_tokens=10000)
        ctx.add_user_message("短消息")
        assert not wm.should_compress()

    def test_should_compress_above(self):
        ctx = ConversationContext()
        wm = WorkingMemory(ctx, max_tokens=50)
        for i in range(10):
            ctx.add_user_message(f"这是一条比较长的消息，用来测试阈值 {i}")
            ctx.add_assistant_message(content=f"收到消息 {i}，让我处理一下")
        assert wm.should_compress()

    def test_should_compress_with_actual_tokens(self):
        ctx = ConversationContext()
        wm = WorkingMemory(ctx, max_tokens=10000)
        ctx.add_user_message("短消息")
        assert not wm.should_compress(actual_tokens=5000)
        assert wm.should_compress(actual_tokens=10000)

    def test_get_current_tokens(self):
        ctx = ConversationContext()
        wm = WorkingMemory(ctx)
        assert wm.get_current_tokens() == 0
        ctx.add_user_message("测试消息")
        assert wm.get_current_tokens() > 0

    def test_running_summary_setter(self):
        ctx = ConversationContext()
        wm = WorkingMemory(ctx)
        assert wm.running_summary is None
        wm.running_summary = "测试摘要"
        assert wm.running_summary == "测试摘要"


class TestSessionSummary:
    def test_to_dict_and_back(self):
        s = SessionSummary(
            session_id="sess-123",
            time_start="2026-02-22T10:00:00",
            time_end="2026-02-22T11:00:00",
            summary="讨论了明天的日程",
            decisions=["下午3点开会"],
            tags=["日程", "会议"],
            turn_count=5,
        )
        d = s.to_dict()
        s2 = SessionSummary.from_dict(d)
        assert s2.session_id == s.session_id
        assert s2.summary == s.summary
        assert s2.decisions == s.decisions
        assert s2.turn_count == 5

    def test_to_markdown(self):
        s = SessionSummary(
            session_id="sess-123",
            time_start="2026-02-22T10:00:00",
            time_end="2026-02-22T11:00:00",
            summary="讨论了项目计划",
            decisions=["采用方案A"],
            open_questions=["预算待确认"],
        )
        md = s.to_markdown()
        assert "sess-123" in md
        assert "讨论了项目计划" in md
        assert "采用方案A" in md
        assert "预算待确认" in md


class TestMemoryEntry:
    def test_to_dict_and_back(self):
        e = MemoryEntry(
            id="mem-abc",
            created_at="2026-02-22T10:00:00",
            source_session_ids=["sess-1", "sess-2"],
            content="用户偏好：下午3点后不安排会议",
            category="preference",
            tags=["偏好", "会议"],
            confidence=0.9,
        )
        d = e.to_dict()
        e2 = MemoryEntry.from_dict(d)
        assert e2.id == e.id
        assert e2.content == e.content
        assert e2.confidence == 0.9

    def test_to_markdown(self):
        e = MemoryEntry(
            id="mem-abc",
            created_at="2026-02-22",
            content="经验教训：需要提前确认会议室",
            category="lesson",
        )
        md = e.to_markdown()
        assert "lesson" in md
        assert "需要提前确认会议室" in md


class TestShortTermMemory:
    def test_add_and_get(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stm = ShortTermMemory(tmpdir, k=5)
            s = SessionSummary(
                session_id="s1",
                time_start="t1",
                time_end="t2",
                summary="test",
            )
            evicted = stm.add(s)
            assert len(evicted) == 0
            assert stm.count == 1
            assert stm.entries[0].session_id == "s1"

    def test_eviction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stm = ShortTermMemory(tmpdir, k=2)
            for i in range(3):
                s = SessionSummary(
                    session_id=f"s{i}",
                    time_start="t",
                    time_end="t",
                    summary=f"summary {i}",
                )
                stm.add(s)
            assert stm.count == 2
            assert stm.entries[0].session_id == "s1"
            assert stm.entries[1].session_id == "s2"

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stm1 = ShortTermMemory(tmpdir, k=10)
            stm1.add(
                SessionSummary(
                    session_id="s1",
                    time_start="t1",
                    time_end="t2",
                    summary="persistent",
                )
            )

            stm2 = ShortTermMemory(tmpdir, k=10)
            assert stm2.count == 1
            assert stm2.entries[0].summary == "persistent"

    def test_search(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stm = ShortTermMemory(tmpdir, k=10)
            stm.add(
                SessionSummary(
                    session_id="s1",
                    time_start="t",
                    time_end="t",
                    summary="讨论了日程安排",
                    tags=["日程"],
                )
            )
            stm.add(
                SessionSummary(
                    session_id="s2",
                    time_start="t",
                    time_end="t",
                    summary="讨论了代码审查",
                    tags=["代码"],
                )
            )
            results = stm.search("日程")
            assert len(results) == 1
            assert results[0].session_id == "s1"

    def test_to_context_string(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stm = ShortTermMemory(tmpdir, k=10)
            stm.add(
                SessionSummary(
                    session_id="s1",
                    time_start="2026-01-01",
                    time_end="t",
                    summary="会话摘要1",
                )
            )
            ctx = stm.to_context_string()
            assert "会话摘要1" in ctx


class TestLongTermMemory:
    def test_read_memory_md_creates_template(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = str(Path(tmpdir) / "MEMORY.md")
            ltm = LongTermMemory(str(Path(tmpdir) / "lt"), md_path)
            content = ltm.read_memory_md()
            assert Path(md_path).exists()
            assert "长期记忆" in content

    def test_search(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ltm = LongTermMemory(str(Path(tmpdir) / "lt"), str(Path(tmpdir) / "M.md"))
            ltm._entries.append(
                MemoryEntry(
                    id="m1",
                    created_at="t",
                    content="用户偏好下午开会",
                    category="preference",
                    tags=["偏好"],
                )
            )
            results = ltm.search("偏好")
            assert len(results) == 1
            assert results[0].id == "m1"


class TestContentMemory:
    def test_ingest_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cm = ContentMemory(tmpdir)
            path = cm.ingest_text("# Test\nHello", "test_note", "notes")
            assert path.exists()
            assert path.read_text() == "# Test\nHello"

    def test_list_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cm = ContentMemory(tmpdir)
            cm.ingest_text("a", "f1", "notes")
            cm.ingest_text("b", "f2", "docs")
            all_files = cm.list_files()
            assert len(all_files) == 2
            notes = cm.list_files("notes")
            assert len(notes) == 1

    def test_search(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cm = ContentMemory(tmpdir)
            cm.ingest_text("# API 认证\n使用 JWT token 进行认证", "api_auth", "docs")
            cm.ingest_text("# 会议记录\n讨论了部署计划", "meeting1", "meeting")
            results = cm.search("API 认证")
            assert len(results) >= 1
            assert "api_auth" in str(results[0][0])

    def test_ingest_md_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cm = ContentMemory(tmpdir)
            src = Path(tmpdir) / "source.md"
            src.write_text("# Source\nContent")
            path = cm.ingest_file(str(src), "docs")
            assert path is not None
            assert path.exists()

    def test_ingest_nonexistent_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cm = ContentMemory(tmpdir)
            assert cm.ingest_file("/nonexistent/file.md", "docs") is None


class TestRecallPolicy:
    def test_should_recall_force_mode(self):
        rp = RecallPolicy(force_recall=True)
        assert rp.should_recall("随便什么")

    def test_should_recall_pattern_match(self):
        rp = RecallPolicy(force_recall=False)
        assert rp.should_recall("你还记得上次我们讨论了什么吗")
        assert rp.should_recall("根据经验应该怎么做")
        assert not rp.should_recall("今天天气怎么样")

    def test_recall_with_long_term(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ltm = LongTermMemory(tmpdir, memory_md_path=str(Path(tmpdir) / "MEMORY.md"))
            rp = RecallPolicy(force_recall=True)
            result = rp.recall("日程", long_term_memory=ltm)
            assert isinstance(result.long_term, list)

    def test_recall_result_to_context_string(self):
        result = RecallResult(
            long_term=[
                MemoryEntry(
                    id="e1",
                    created_at="",
                    source_session_ids=[],
                    content="偏好工作日安排会议",
                    category="preference",
                    tags=[],
                    confidence=0.9,
                ),
            ],
            content=[("path/to/notes.md", "snippet 内容")],
        )
        ctx = result.to_context_string()
        assert "偏好工作日安排会议" in ctx
        assert "path/to/notes.md" in ctx

    def test_recall_result_empty(self):
        result = RecallResult()
        assert result.is_empty()
        assert result.to_context_string() == ""
