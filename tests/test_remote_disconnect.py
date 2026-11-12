"""回归：远程 worker 断连时，bash 工具必须返回可重试的工具错误，而不是抛异常。

背景（2026-11-12 16:20）：worker 掉线的那一分钟里，一次 bash 调用抛了未捕获的
RuntimeError，整个回合中断——用户看到的就是「任务突然停止」。
"""
from __future__ import annotations

import pytest

from agent_core.tools import bash_tool as mod


class _FakeRemoteState:
    login = "sii-jyj"
    requested_path = "~"
    resolved_path = "~"
    profile = "dev"


@pytest.mark.asyncio
async def test_remote_disconnect_returns_retryable_error(monkeypatch) -> None:
    tool = object.__new__(mod.BashTool)

    async def boom(**kwargs):
        raise RuntimeError("远程 worker 执行失败: remote worker disconnected")

    monkeypatch.setattr(tool, "_remote_exec_with_reopen", boom)

    result = await tool._execute_remote_sync(
        command="echo hi",
        timeout=30,
        wait_window_seconds=10,
        wait_for_completion=True,
        remote_state=_FakeRemoteState(),
        exec_ctx={"session_id": "test-session"},
    )

    assert result.success is False
    assert result.error == "REMOTE_WORKER_UNAVAILABLE"
    assert result.data.get("retryable") is True
    assert "sii-jyj" in result.message
