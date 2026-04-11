"""水源 subagent inject 轮询与启发式单元测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_core.interfaces import AgentRunResult
from frontend.shuiyuan_integration.session import (
    _SHUIYUAN_INJECT_CHUNK_SEP,
    _poll_shuiyuan_inject_edits,
    _should_poll_inject_followup,
)


def test_should_poll_when_subagent_running_cn():
    assert _should_poll_inject_followup(
        AgentRunResult(output_text="创建了 subagent，运行中喵！")
    )


def test_should_poll_when_create_subagent_in_text():
    assert _should_poll_inject_followup(
        AgentRunResult(output_text="call_tool create_subagent …")
    )


def test_should_poll_parallel_subagents():
    assert _should_poll_inject_followup(
        AgentRunResult(output_text="已调用 create_parallel_subagents，三路子任务运行中")
    )


def test_should_not_poll_plain_reply():
    assert not _should_poll_inject_followup(
        AgentRunResult(output_text="今天水源首页挺热闹的。")
    )


@pytest.mark.asyncio
async def test_poll_edits_first_inject_after_many_empty(monkeypatch):
    """首段 inject 前大量空 poll：收到后立刻 edit 一次，再按 idle 退出。"""
    n = {"i": 0}
    tick = {"t": 0.0}

    def monotonic() -> float:
        return tick["t"]

    async def poll_push():
        n["i"] += 1
        if n["i"] < 25:
            return []
        if n["i"] == 25:
            return [{"output_text": "子任务完成后的父会话回复"}]
        return []

    async def fake_sleep(_d: object = None) -> None:
        tick["t"] += 1.0

    edits: list[str] = []

    def fake_update(*, post_id: int, raw: str, client: object) -> tuple[bool, str]:
        edits.append(raw)
        return True, ""

    monkeypatch.setattr("frontend.shuiyuan_integration.session.time.monotonic", monotonic)
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.session._SHUIYUAN_INJECT_MAX_WALL_SEC",
        60.0,
    )
    monkeypatch.setattr("frontend.shuiyuan_integration.session.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.reply.update_post_reply",
        fake_update,
    )

    ipc = SimpleNamespace(poll_push=poll_push)
    base = "首帖正文"
    merged = await _poll_shuiyuan_inject_edits(
        ipc,  # type: ignore[arg-type]
        post_id=999,
        client=MagicMock(),
        base_text=base,
    )
    assert merged == base + _SHUIYUAN_INJECT_CHUNK_SEP + "子任务完成后的父会话回复"
    assert len(edits) == 1
    assert edits[0] == merged


@pytest.mark.asyncio
async def test_poll_parallel_inject_two_separate_edits(monkeypatch):
    """两段 inject 间隔大量空 poll：应触发两次 update，最终正文含 A 与 B。"""
    n = {"i": 0}
    tick = {"t": 0.0}

    def monotonic() -> float:
        return tick["t"]

    async def poll_push():
        n["i"] += 1
        if n["i"] == 1:
            return [{"output_text": "A"}]
        if n["i"] < 40:
            return []
        if n["i"] == 40:
            return [{"output_text": "B"}]
        return []

    async def fake_sleep(_d: object = None) -> None:
        tick["t"] += 1.0

    edits: list[str] = []

    def fake_update(*, post_id: int, raw: str, client: object) -> tuple[bool, str]:
        edits.append(raw)
        return True, ""

    monkeypatch.setattr("frontend.shuiyuan_integration.session.time.monotonic", monotonic)
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.session._SHUIYUAN_INJECT_MAX_WALL_SEC",
        120.0,
    )
    monkeypatch.setattr("frontend.shuiyuan_integration.session.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.reply.update_post_reply",
        fake_update,
    )

    ipc = SimpleNamespace(poll_push=poll_push)
    base = "X"
    merged = await _poll_shuiyuan_inject_edits(
        ipc,  # type: ignore[arg-type]
        post_id=1,
        client=MagicMock(),
        base_text=base,
    )
    assert len(edits) == 2
    assert "A" in merged and "B" in merged
    assert edits[-1] == merged


@pytest.mark.asyncio
async def test_poll_exits_after_idle_without_waiting_max_wall(monkeypatch):
    """一段 inject 后空闲超过 idle 即停轮询；已对该段做过 edit。"""
    n = {"i": 0}
    tick = {"t": 0.0}

    def monotonic() -> float:
        return tick["t"]

    async def poll_push():
        n["i"] += 1
        if n["i"] == 1:
            return [{"output_text": "inject 完成"}]
        return []

    async def fake_sleep(_d: object = None) -> None:
        tick["t"] += 10.0

    def fake_update(*, post_id: int, raw: str, client: object) -> tuple[bool, str]:
        return True, ""

    monkeypatch.setattr("frontend.shuiyuan_integration.session.time.monotonic", monotonic)
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.session._SHUIYUAN_INJECT_MAX_WALL_SEC",
        99999.0,
    )
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.session._SHUIYUAN_INJECT_IDLE_AFTER_LAST_CHUNK_SEC",
        25.0,
    )
    monkeypatch.setattr("frontend.shuiyuan_integration.session.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "frontend.shuiyuan_integration.reply.update_post_reply",
        fake_update,
    )

    ipc = SimpleNamespace(poll_push=poll_push)
    merged = await _poll_shuiyuan_inject_edits(
        ipc,  # type: ignore[arg-type]
        post_id=1,
        client=MagicMock(),
        base_text="base",
    )
    assert "inject 完成" in merged
