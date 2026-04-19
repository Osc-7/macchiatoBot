"""
``agent_core.agent.tool_result_overflow`` 单测。

覆盖：
* 阈值未触发时原样返回；
* 触发时落盘到指定目录、message 含截断标记、新 ToolResult 估算 token ≤ 上限；
* 落盘失败（目录不可写）时仍做截断、不抛异常；
* 管理员模式下落盘到 admin_overflow_dir，marker 用绝对路径；
* ``estimate_result_tokens`` 与 ``_truncate_string_to_tokens`` 边界。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.agent.tool_result_overflow import (
    OverflowOutcome,
    estimate_result_tokens,
    maybe_offload_tool_result,
    _truncate_string_to_tokens,
)
from agent_core.memory.working_memory import estimate_tokens
from agent_core.tools.base import ToolResult


def _big_text(approx_tokens: int) -> str:
    """生成估算约 ``approx_tokens`` 的英文文本（4 char/token）。"""
    target_chars = approx_tokens * 4
    chunk = "abcdefghij" * 10  # 100 chars
    return (chunk * (target_chars // 100 + 1))[:target_chars]


class TestTruncateStringToTokens:
    def test_short_text_returned_unchanged(self):
        assert _truncate_string_to_tokens("hello", target_tokens=100) == "hello"

    def test_zero_target_returns_empty(self):
        assert _truncate_string_to_tokens("hello world", target_tokens=0) == ""

    def test_long_text_truncated_under_target(self):
        text = _big_text(2000)
        out = _truncate_string_to_tokens(text, target_tokens=500)
        assert estimate_tokens(out) <= 500
        assert text.startswith(out)
        assert len(out) > 0

    def test_chinese_text_truncated_under_target(self):
        text = "中文" * 5000  # ~6666 tokens（每字 ~0.66 token，10000 字）
        out = _truncate_string_to_tokens(text, target_tokens=300)
        assert estimate_tokens(out) <= 300
        assert text.startswith(out)


class TestMaybeOffloadToolResult:
    def test_under_threshold_returned_as_is(self, tmp_path: Path):
        result = ToolResult(success=True, message="ok", data={"x": "small"})
        new_result, outcome = maybe_offload_tool_result(
            result,
            tool_name="tool_a",
            tool_call_id="call_1",
            workspace_dir=str(tmp_path),
            max_tokens=10_000,
        )
        assert new_result is result, "未触发时应返回原对象"
        assert outcome == OverflowOutcome(triggered=False, original_tokens=outcome.original_tokens)
        assert outcome.overflow_path is None

    def test_disabled_when_max_tokens_zero(self, tmp_path: Path):
        result = ToolResult(success=True, message="ok", data=_big_text(50_000))
        new_result, outcome = maybe_offload_tool_result(
            result,
            tool_name="tool_b",
            tool_call_id="call_2",
            workspace_dir=str(tmp_path),
            max_tokens=0,
        )
        assert new_result is result
        assert outcome.triggered is False

    def test_disabled_when_max_tokens_none(self, tmp_path: Path):
        result = ToolResult(success=True, message="ok", data=_big_text(50_000))
        new_result, outcome = maybe_offload_tool_result(
            result,
            tool_name="tool_b",
            tool_call_id="call_3",
            workspace_dir=str(tmp_path),
            max_tokens=None,
        )
        assert new_result is result
        assert outcome.triggered is False

    def test_over_threshold_triggers_truncation_and_persistence(self, tmp_path: Path):
        big = _big_text(40_000)
        result = ToolResult(
            success=True,
            message="搜索完成",
            data={"raw": big},
            metadata={"source": "web"},
        )
        original_tokens = estimate_result_tokens(result)
        assert original_tokens > 30_000

        new_result, outcome = maybe_offload_tool_result(
            result,
            tool_name="web_search",
            tool_call_id="call_abc-123/xyz",
            workspace_dir=str(tmp_path),
            max_tokens=5_000,
        )

        assert outcome.triggered is True
        assert outcome.overflow_path is not None
        assert outcome.overflow_path.exists()
        # 文件名应安全化：斜杠/破折号被规整
        assert "_" in outcome.overflow_path.name
        assert outcome.overflow_path.parent == (tmp_path / ".tool_results")

        # 文件内容是原始 JSON（包含完整数据，AI 可按需取回）
        full = json.loads(outcome.overflow_path.read_text(encoding="utf-8"))
        assert full["data"]["raw"] == big

        # 截断后 message 含显式标记 + 相对路径
        assert "已截断" in new_result.message
        assert ".tool_results/" in new_result.message
        assert outcome.display_path == f".tool_results/{outcome.overflow_path.name}"

        # 截断后 to_json 估算 ≤ 上限（核心保护目标）
        kept = estimate_result_tokens(new_result)
        assert kept <= 5_000, f"kept={kept} 超过上限 5000"
        assert outcome.kept_tokens == kept

        # data 替换为结构化 dict，AI 既能看到 head preview 也知道路径
        assert new_result.data["truncated"] is True
        assert new_result.data["original_tokens"] == original_tokens
        assert new_result.data["overflow_path"] == outcome.display_path
        assert isinstance(new_result.data["preview"], str) and len(new_result.data["preview"]) > 0

        # 保留 success / error / 用户 metadata，并注入 _overflow 审计字段
        assert new_result.success is True
        assert new_result.metadata["source"] == "web"
        assert new_result.metadata["_overflow"]["triggered"] is True
        assert new_result.metadata["_overflow"]["original_tokens"] == original_tokens

    def test_persist_failure_still_truncates(self, tmp_path: Path, monkeypatch):
        """目录无法 mkdir 时仍做截断，message 给降级标记，不抛异常。"""
        result = ToolResult(success=True, message="ok", data=_big_text(20_000))
        # 用一个文件占住目标路径，导致 mkdir 失败
        bad_parent = tmp_path / "blocked"
        bad_parent.write_text("i am a file, not a dir")

        new_result, outcome = maybe_offload_tool_result(
            result,
            tool_name="tool_c",
            tool_call_id="call_4",
            workspace_dir=str(bad_parent),  # mkdir(bad_parent / .tool_results) 会失败
            max_tokens=2_000,
        )

        assert outcome.triggered is True
        assert outcome.overflow_path is None
        assert "已截断" in new_result.message
        assert "落盘失败" in new_result.message
        # 即便落盘失败，也应保证 to_json 不超阈值
        assert estimate_result_tokens(new_result) <= 2_000

    def test_admin_mode_uses_admin_overflow_dir_with_absolute_path(self, tmp_path: Path):
        """管理员模式下转储到 admin_overflow_dir，marker 给绝对路径。"""
        big = _big_text(30_000)
        result = ToolResult(success=True, message="ok", data={"raw": big})

        admin_dir = tmp_path / "tmp_admin" / ".tool_results"
        new_result, outcome = maybe_offload_tool_result(
            result,
            tool_name="tool_d",
            tool_call_id="call_5",
            workspace_dir=str(tmp_path / "should_not_be_used"),
            max_tokens=4_000,
            is_workspace_admin=True,
            admin_overflow_dir=str(admin_dir),
        )

        assert outcome.triggered is True
        assert outcome.overflow_path is not None
        # 转储到 admin 目录而非工作区
        assert outcome.overflow_path.parent == admin_dir.resolve()
        # marker 用绝对路径
        assert outcome.display_path == str(outcome.overflow_path.resolve())
        assert outcome.display_path in new_result.message
