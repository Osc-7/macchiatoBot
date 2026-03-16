"""Runtime behavior tests for kernel scheduler/core pool."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.context import ConversationContext
from agent_core.kernel_interface import CoreProfile, KernelRequest
from agent_core.tools import VersionedToolRegistry
from system.kernel import AgentKernel, CoreEntry, CorePool, KernelScheduler


@pytest.mark.asyncio
async def test_priority_queue_inject_before_user_request() -> None:
    """验证 PriorityQueue 调度顺序：priority=-1（inject）应先于 priority=0（用户请求）被处理。"""
    queue: asyncio.PriorityQueue[KernelRequest] = asyncio.PriorityQueue()

    # 先入队用户请求（priority=0），再入队 inject（priority=-1）
    user_req = KernelRequest.create(
        text="用户消息",
        session_id="cli:default",
        priority=0,
    )
    inject_req = KernelRequest.create(
        text="[子任务 abc 完成]\n\n结果",
        session_id="cli:default",
        frontend_id="subagent",
        priority=-1,
    )
    await queue.put(user_req)
    await queue.put(inject_req)

    # get() 应返回最小的（priority 最小 = 最高优先级）
    first = await queue.get()
    assert first.priority == -1, "inject（priority=-1）应先于用户请求（priority=0）被处理"
    assert "子任务" in (first.text or "")

    second = await queue.get()
    assert second.priority == 0
    assert second.text == "用户消息"


@pytest.mark.asyncio
async def test_kernel_request_ordering_by_priority_then_enqueued_at() -> None:
    """验证 KernelRequest 比较顺序：priority 优先，同优先级按 enqueued_at FIFO。"""
    t = time.monotonic()
    r_low = KernelRequest(priority=0, enqueued_at=t + 1, request_id="a")
    r_high = KernelRequest(priority=-1, enqueued_at=t + 2, request_id="b")
    assert r_high < r_low, "priority 越小越优先"

    r_first = KernelRequest(priority=0, enqueued_at=t, request_id="a")
    r_second = KernelRequest(priority=0, enqueued_at=t + 1, request_id="b")
    assert r_first < r_second, "同优先级按 enqueued_at FIFO"


@pytest.mark.asyncio
async def test_scheduler_ttl_does_not_evict_inflight_session() -> None:
    core_pool = SimpleNamespace(
        scan_expired=lambda: ["s1"],
        evict=AsyncMock(),
        touch=lambda _sid: None,
    )
    scheduler = KernelScheduler(
        kernel=SimpleNamespace(),  # type: ignore[arg-type]
        core_pool=core_pool,  # type: ignore[arg-type]
    )

    scheduler._inflight_sessions["s1"] = 1  # type: ignore[attr-defined]
    await scheduler._evict_expired()
    core_pool.evict.assert_not_awaited()

    scheduler._inflight_sessions["s1"] = 0  # type: ignore[attr-defined]
    await scheduler._evict_expired()
    core_pool.evict.assert_awaited_once_with("s1")


@pytest.mark.asyncio
async def test_core_pool_acquire_hot_updates_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_old = CoreProfile.default_full(frontend_id="cli", dialog_window_id="u1")
    profile_new = CoreProfile.default_sub(
        allowed_tools=["parse_time"],
        frontend_id="wechat",
        dialog_window_id="u2",
    )
    fake_registry = object()

    captured: dict = {}

    def _fake_build_tool_registry(*, profile=None, config=None, memory_owner_id=None, subagent_registry=None, core_pool=None):  # type: ignore[no-untyped-def]
        captured["profile"] = profile
        captured["memory_owner_id"] = memory_owner_id
        return fake_registry

    monkeypatch.setattr("system.tools.build_tool_registry", _fake_build_tool_registry)

    pool = CorePool()
    fake_agent = SimpleNamespace(
        _tool_registry=VersionedToolRegistry(),
        _source="cli",
        _user_id="u1",
        _core_profile=profile_old,
        _session_id="sess-1",
    )
    pool._pool["sess-1"] = CoreEntry(agent=fake_agent, profile=profile_old)

    agent = await pool.acquire(
        "sess-1",
        source="wechat",
        user_id="u2",
        profile=profile_new,
    )

    assert agent is fake_agent
    assert fake_agent._tool_registry is fake_registry
    assert fake_agent._source == "wechat"
    assert fake_agent._user_id == "u2"
    assert fake_agent._core_profile == profile_new
    assert pool._pool["sess-1"].profile == profile_new
    assert captured["profile"] == profile_new
    assert captured["memory_owner_id"] == "u2"


@pytest.mark.asyncio
async def test_scheduler_get_session_lock_serializes_same_session() -> None:
    """同一 session 的多个并发请求应通过 per-session lock 串行化。"""
    core_pool = SimpleNamespace(
        scan_expired=lambda: [],
        evict=AsyncMock(),
        touch=lambda _sid: None,
        list_sessions=lambda: [],
    )
    scheduler = KernelScheduler(
        kernel=SimpleNamespace(),  # type: ignore[arg-type]
        core_pool=core_pool,  # type: ignore[arg-type]
    )

    lock1 = await scheduler._get_session_lock("sess-A")
    lock2 = await scheduler._get_session_lock("sess-A")
    lock3 = await scheduler._get_session_lock("sess-B")

    # 同 session 应返回同一个 Lock 对象
    assert lock1 is lock2
    # 不同 session 应返回不同 Lock
    assert lock1 is not lock3


@pytest.mark.asyncio
async def test_scheduler_concurrent_requests_same_session_serialized() -> None:
    """并发发出同一 session 的两个请求，第二个应等待第一个完成后才执行。"""
    execution_order: list[str] = []
    barrier = asyncio.Event()

    async def slow_process_first() -> None:
        execution_order.append("first_start")
        await barrier.wait()  # 等待信号才继续
        execution_order.append("first_end")

    async def fast_process_second() -> None:
        execution_order.append("second_start")
        execution_order.append("second_end")

    core_pool = SimpleNamespace(
        scan_expired=lambda: [],
        evict=AsyncMock(),
        touch=lambda _sid: None,
        list_sessions=lambda: [],
    )
    scheduler = KernelScheduler(
        kernel=SimpleNamespace(),  # type: ignore[arg-type]
        core_pool=core_pool,  # type: ignore[arg-type]
    )

    lock = await scheduler._get_session_lock("sess-X")

    async def first_task() -> None:
        async with lock:
            await slow_process_first()

    async def second_task() -> None:
        async with lock:
            await fast_process_second()

    # 启动两个任务，first_task 先持有锁并阻塞
    t1 = asyncio.create_task(first_task())
    await asyncio.sleep(0)  # 让 first_task 先进入 lock
    t2 = asyncio.create_task(second_task())
    await asyncio.sleep(0)  # 让 second_task 尝试获取 lock（会阻塞）

    # 此时 first_task 在 barrier.wait()，second_task 在等锁
    assert execution_order == ["first_start"]

    # 释放 barrier，first_task 完成，second_task 开始
    barrier.set()
    await asyncio.gather(t1, t2)

    assert execution_order == ["first_start", "first_end", "second_start", "second_end"]


@pytest.mark.asyncio
async def test_agent_prepare_turn_populates_recall_result(tmp_path) -> None:
    """prepare_turn 应在所有路径中执行 memory recall（包括 scheduler 路径之前缺失的情况）。"""
    from agent_core.agent.agent import AgentCore
    from agent_core.config import Config

    # 构造最小可用 Config（memory disabled，避免创建目录）
    config = MagicMock(spec=Config)
    config.llm = MagicMock()
    config.llm.summary_model = None
    config.agent = MagicMock()
    config.agent.tool_mode = "kernel"
    config.agent.source_overrides = {}
    config.agent.pinned_tools = []
    config.agent.working_set_size = 6
    config.agent.max_iterations = 10
    config.memory = MagicMock()
    config.memory.enabled = False
    config.memory.max_working_tokens = 4000
    config.memory.working_summary_threshold = 0.8
    config.memory.working_keep_recent = 5
    config.memory.working_summary_hard_ratio = 0.9
    config.memory.force_recall = False
    config.memory.recall_top_n = 3
    config.memory.recall_score_threshold = 0.5
    config.mcp = MagicMock()
    config.mcp.enabled = False
    config.time = MagicMock()
    config.time.timezone = "Asia/Shanghai"

    with patch("agent_core.agent.agent.LLMClient"):
        # memory_enabled=True 时 recall 路径应被执行
        config.memory.enabled = True
        agent = AgentCore(config=config, tools=[], memory_enabled=True)

    # 记录 recall 是否被调用（memory_enabled=True 时 prepare_turn 应进入 recall 分支）
    recall_called = False

    def mock_should_recall(text: str) -> bool:
        nonlocal recall_called
        recall_called = True
        return False  # 不实际执行 recall，只验证被调用

    agent._recall_policy.should_recall = mock_should_recall  # type: ignore[method-assign]
    # mock ChatHistoryDB.write_message 避免真实 IO
    if agent._chat_history_db is not None:
        agent._chat_history_db.write_message = MagicMock(return_value=1)  # type: ignore[method-assign]

    turn_id, summary_task, summary_recent_start = await agent.prepare_turn("测试消息")

    assert turn_id == 1
    assert summary_task is None
    assert summary_recent_start is None
    assert recall_called, "prepare_turn 在 memory_enabled=True 时应调用 recall_policy.should_recall"
    assert len(agent._context.get_messages()) == 1


@pytest.mark.asyncio
async def test_compress_context_keeps_complete_recent_turn() -> None:
    registry = VersionedToolRegistry()
    kernel = AgentKernel(tool_registry=registry)

    ctx = ConversationContext()
    ctx.messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"ok":true}'},
        {"role": "assistant", "content": "a2"},
    ]
    agent = SimpleNamespace(_context=ctx, _summary_llm_client=None)

    summary, kept = await kernel._compress_context(agent, keep_recent_turns=1)

    assert summary == ""
    assert kept == 4
    assert [m["role"] for m in ctx.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert ctx.messages[0]["content"] == "u2"
