"""Runtime behavior tests for kernel scheduler/core pool."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.interfaces import AgentRunResult
from agent_core.config import CommandToolsConfig, Config, LLMConfig
from agent_core.context import ConversationContext
from agent_core.kernel_interface import CoreProfile, CoreStatsAction, KernelRequest
from agent_core.orchestrator import ToolWorkingSetManager
from agent_core.tools import VersionedToolRegistry
from system.kernel import AgentKernel, CoreEntry, CorePool, KernelScheduler
from system.kernel.scheduler import memory_owner_for_kernel_acquire
from system.kernel.summarizer import SessionSummarizer
from system.multi_agent.constants import (
    METADATA_KEY_AGENT_MESSAGE,
    P2P_REQUEST_FRONTEND_TAG,
)


def test_memory_owner_agent_msg_uses_session_id_not_frontend_tag() -> None:
    """P2P 的 frontend_id=agent_msg 不得作为 acquire source（避免 session-agent_msg:* 误日志）。"""
    req = KernelRequest.create(
        text="hi",
        session_id="sub:abc-uuid",
        frontend_id=P2P_REQUEST_FRONTEND_TAG,
        metadata={METADATA_KEY_AGENT_MESSAGE: {}},
    )
    src, uid = memory_owner_for_kernel_acquire(req)
    assert src == "subagent"
    assert uid == "abc-uuid"

    req2 = KernelRequest.create(
        text="hi",
        session_id="feishu:ou_test",
        frontend_id=P2P_REQUEST_FRONTEND_TAG,
        metadata={},
    )
    assert memory_owner_for_kernel_acquire(req2) == ("feishu", "ou_test")

    req2b = KernelRequest.create(
        text="hi",
        session_id="feishu:user:ou_test:1779373360",
        frontend_id=P2P_REQUEST_FRONTEND_TAG,
        metadata={},
    )
    assert memory_owner_for_kernel_acquire(req2b) == ("feishu", "ou_test")

    req3 = KernelRequest.create(
        text="hi",
        session_id="cli:root",
        frontend_id=P2P_REQUEST_FRONTEND_TAG,
    )
    assert memory_owner_for_kernel_acquire(req3) == ("cli", "root")


@pytest.mark.asyncio
async def test_kernel_propagates_return_status_in_metadata() -> None:
    from agent_core.kernel_interface.action import ReturnAction
    from system.kernel.kernel import AgentKernel

    async def _fake_run_loop(**_kwargs):
        yield ReturnAction(message="ok", status="completed")

    registry = VersionedToolRegistry()
    kernel = AgentKernel(tool_registry=registry)
    agent = SimpleNamespace(
        run_loop=_fake_run_loop,
        _current_visible_tools=set(),
    )

    result = await kernel.run(agent, turn_id=1)  # type: ignore[arg-type]
    assert result.metadata.get("status") == "completed"


def test_subagent_memory_owner_inherits_parent_namespace() -> None:
    """子 Agent 可通过 metadata.memory_owner 继承父会话的权限/工作区命名空间。"""
    profile = CoreProfile.default_sub(
        allowed_tools=None,
        frontend_id="subagent",
        dialog_window_id="abc-uuid",
    )
    req = KernelRequest.create(
        text="task",
        session_id="sub:abc-uuid",
        frontend_id="subagent",
        metadata={"memory_owner": "feishu:ou_parent"},
        profile=profile,
    )
    assert memory_owner_for_kernel_acquire(req) == ("feishu", "ou_parent")


@pytest.mark.asyncio
async def test_evict_shutdown_calls_flush_checkpoint() -> None:
    """shutdown=True 时应在 close 前刷新 checkpoint，避免恢复时 elapsed 误判。"""
    agent = MagicMock()
    agent.flush_checkpoint_for_shutdown = MagicMock()
    pool = CorePool()
    profile = CoreProfile.default_full(frontend_id="cli", dialog_window_id="root")
    pool._pool["cli:root"] = CoreEntry(agent=agent, profile=profile)
    pool._kernel = MagicMock()
    pool._kernel.kill = AsyncMock(return_value=None)
    pool._summarizer = None

    await pool.evict("cli:root", shutdown=True)

    agent.flush_checkpoint_for_shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_evict_normal_does_not_call_flush_checkpoint() -> None:
    agent = MagicMock()
    agent.flush_checkpoint_for_shutdown = MagicMock()
    pool = CorePool()
    profile = CoreProfile.default_full(frontend_id="cli", dialog_window_id="root")
    pool._pool["cli:root"] = CoreEntry(agent=agent, profile=profile)
    pool._kernel = MagicMock()
    pool._kernel.kill = AsyncMock(return_value=None)
    pool._summarizer = None

    await pool.evict("cli:root", shutdown=False)

    agent.flush_checkpoint_for_shutdown.assert_not_called()


@pytest.mark.asyncio
async def test_evict_ttl_preserves_active_remote_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTL 空闲回收只丢本地 Core，远程绑定应保持到 /remote-release 或 remote TTL。"""
    from agent_core.remote.workspace_state import (
        activate_remote_workspace,
        clear_remote_workspace_state,
        get_remote_workspace_state,
    )

    clear_remote_workspace_state()
    activate_remote_workspace(
        session_id="cli:root",
        login="laptop",
        requested_path="~/Project",
        ttl_seconds=None,
    )

    close_workspace = AsyncMock()
    monkeypatch.setattr(
        "agent_core.remote.worker_registry.get_remote_worker_registry",
        lambda: SimpleNamespace(close_workspace=close_workspace),
    )
    detach_all = AsyncMock()
    monkeypatch.setattr(
        "agent_core.mcp.session_overlay.get_mcp_session_overlay",
        lambda: SimpleNamespace(detach_all_remote=detach_all),
    )

    agent = MagicMock()
    agent.close = AsyncMock()
    pool = CorePool()
    profile = CoreProfile.default_full(frontend_id="cli", dialog_window_id="root")
    pool._pool["cli:root"] = CoreEntry(agent=agent, profile=profile)
    pool._kernel = MagicMock()
    pool._kernel.kill = AsyncMock(return_value=None)
    pool._summarizer = None

    await pool.evict("cli:root", shutdown=False)

    detach_all.assert_not_awaited()
    close_workspace.assert_not_awaited()
    state = get_remote_workspace_state("cli:root")
    assert state is not None
    assert state.login == "laptop"


@pytest.mark.asyncio
async def test_evict_release_remote_closes_worker_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式结束会话时应关闭 worker 远程会话。"""
    from agent_core.remote.workspace_state import (
        activate_remote_workspace,
        clear_remote_workspace_state,
        get_remote_workspace_state,
    )

    clear_remote_workspace_state()
    activate_remote_workspace(
        session_id="cli:root",
        login="laptop",
        requested_path="~/Project",
        ttl_seconds=None,
    )

    close_workspace = AsyncMock()
    monkeypatch.setattr(
        "agent_core.remote.worker_registry.get_remote_worker_registry",
        lambda: SimpleNamespace(close_workspace=close_workspace),
    )
    detach_all = AsyncMock()
    monkeypatch.setattr(
        "agent_core.mcp.session_overlay.get_mcp_session_overlay",
        lambda: SimpleNamespace(detach_all_remote=detach_all),
    )

    agent = MagicMock()
    agent.close = AsyncMock()
    pool = CorePool()
    profile = CoreProfile.default_full(frontend_id="cli", dialog_window_id="root")
    pool._pool["cli:root"] = CoreEntry(agent=agent, profile=profile)
    pool._kernel = MagicMock()
    pool._kernel.kill = AsyncMock(return_value=None)
    pool._summarizer = None

    await pool.evict("cli:root", shutdown=False, release_remote=True)

    detach_all.assert_awaited_once_with(agent, session_id="cli:root")
    close_workspace.assert_awaited_once_with(login="laptop", session_id="cli:root")
    assert get_remote_workspace_state("cli:root") is None


@pytest.mark.asyncio
async def test_evict_shutdown_releases_remote_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kernel/daemon 停机（shutdown=True）默认释放远程工作区。"""
    from agent_core.remote.workspace_state import (
        activate_remote_workspace,
        clear_remote_workspace_state,
        get_remote_workspace_state,
    )

    clear_remote_workspace_state()
    activate_remote_workspace(
        session_id="cli:root",
        login="laptop",
        requested_path="~/Project",
        ttl_seconds=None,
    )

    close_workspace = AsyncMock()
    monkeypatch.setattr(
        "agent_core.remote.worker_registry.get_remote_worker_registry",
        lambda: SimpleNamespace(close_workspace=close_workspace),
    )
    monkeypatch.setattr(
        "agent_core.mcp.session_overlay.get_mcp_session_overlay",
        lambda: SimpleNamespace(detach_all_remote=AsyncMock()),
    )

    agent = MagicMock()
    agent.close = AsyncMock()
    agent.flush_checkpoint_for_shutdown = MagicMock()
    pool = CorePool()
    profile = CoreProfile.default_full(frontend_id="cli", dialog_window_id="root")
    pool._pool["cli:root"] = CoreEntry(agent=agent, profile=profile)
    pool._kernel = MagicMock()
    pool._kernel.kill = AsyncMock(return_value=None)
    pool._summarizer = None

    await pool.evict("cli:root", shutdown=True)

    close_workspace.assert_awaited_once_with(login="laptop", session_id="cli:root")
    assert get_remote_workspace_state("cli:root") is None


@pytest.mark.asyncio
async def test_evict_passes_normal_system_prompt_to_session_summarizer() -> None:
    agent = MagicMock()
    agent._context.get_messages.return_value = [{"role": "user", "content": "hi"}]
    agent._build_system_prompt.return_value = "普通 system"
    agent._user_id = "root"
    pool = CorePool()
    profile = CoreProfile.default_full(frontend_id="cli", dialog_window_id="root")
    pool._pool["cli:root"] = CoreEntry(agent=agent, profile=profile)
    pool._kernel = MagicMock()
    pool._kernel.kill = AsyncMock(
        return_value=CoreStatsAction(session_id="cli:root", turn_count=1)
    )
    pool._summarizer = MagicMock()
    pool._summarizer.summarize_and_persist = AsyncMock()

    await pool.evict("cli:root", shutdown=False)

    pool._summarizer.summarize_and_persist.assert_awaited_once()
    assert (
        pool._summarizer.summarize_and_persist.await_args.kwargs["system_message"]
        == "普通 system"
    )


def test_kernel_parse_arguments_success_and_failure() -> None:
    """流式解析失败时不应静默得到空 dict，应返回明确错误信息。"""
    # 正常 dict
    parsed, err = AgentKernel._parse_arguments({"path": "a.md", "content": "x"})
    assert err is None
    assert parsed == {"path": "a.md", "content": "x"}

    # 正常 JSON 字符串
    parsed, err = AgentKernel._parse_arguments('{"path": "b.md"}')
    assert err is None
    assert parsed == {"path": "b.md"}

    # 空字符串
    parsed, err = AgentKernel._parse_arguments("")
    assert err is not None
    assert "空" in err
    assert parsed == {}

    # 无效 JSON（模拟流式截断）
    parsed, err = AgentKernel._parse_arguments('{"path": "')
    assert err is not None
    assert "解析" in err or "截断" in err
    assert parsed == {}


@pytest.mark.asyncio
async def test_priority_queue_inject_before_user_request() -> None:
    """验证 PriorityQueue 调度顺序：priority=-1（inject）应先于 priority=0（用户请求）被处理。"""
    queue: asyncio.PriorityQueue[KernelRequest] = asyncio.PriorityQueue()

    # 先入队用户请求（priority=0），再入队 inject（priority=-1）
    user_req = KernelRequest.create(
        text="用户消息",
        session_id="cli:root",
        priority=0,
    )
    inject_req = KernelRequest.create(
        text="[子任务 abc 完成]\n\n结果",
        session_id="cli:root",
        frontend_id="subagent",
        priority=-1,
    )
    await queue.put(user_req)
    await queue.put(inject_req)

    # get() 应返回最小的（priority 最小 = 最高优先级）
    first = await queue.get()
    assert (
        first.priority == -1
    ), "inject（priority=-1）应先于用户请求（priority=0）被处理"
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
    fake_registry = VersionedToolRegistry()

    captured: dict = {}

    def _fake_build_tool_registry(*, profile=None, config=None, memory_owner_id=None, core_pool=None, **kwargs):  # type: ignore[no-untyped-def]
        captured["profile"] = profile
        captured["memory_owner_id"] = memory_owner_id
        return fake_registry

    monkeypatch.setattr("system.tools.build_tool_registry", _fake_build_tool_registry)

    pool = CorePool()
    old_registry = VersionedToolRegistry()
    old_bash_tool = SimpleNamespace(name="bash")
    old_registry.register(old_bash_tool)
    fake_agent = SimpleNamespace(
        _tool_registry=old_registry,
        _tool_catalog=VersionedToolRegistry(),
        _working_set=ToolWorkingSetManager(
            pinned_tools=["search_tools", "call_tool", "bash"]
        ),
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
    assert isinstance(fake_agent._tool_registry, VersionedToolRegistry)
    assert fake_agent._tool_catalog is fake_registry
    assert fake_agent._tool_registry.has("bash") is True
    assert fake_agent._tool_catalog.has("bash") is True
    assert fake_agent._source == "wechat"
    assert fake_agent._user_id == "u2"
    assert fake_agent._core_profile == profile_new
    assert pool._pool["sess-1"].profile == profile_new
    assert captured["profile"] == profile_new
    assert captured["memory_owner_id"] == "u2"


@pytest.mark.asyncio
async def test_core_pool_acquire_rebinds_owner_without_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = CoreProfile.default_full(frontend_id="cli", dialog_window_id="u1")
    fake_registry = VersionedToolRegistry()
    captured: dict = {}

    def _fake_build_tool_registry(*, profile=None, config=None, memory_owner_id=None, core_pool=None, **kwargs):  # type: ignore[no-untyped-def]
        captured["profile"] = profile
        captured["memory_owner_id"] = memory_owner_id
        return fake_registry

    monkeypatch.setattr("system.tools.build_tool_registry", _fake_build_tool_registry)

    pool = CorePool()
    old_registry = VersionedToolRegistry()
    old_bash_tool = SimpleNamespace(name="bash")
    old_registry.register(old_bash_tool)
    fake_agent = SimpleNamespace(
        _tool_registry=old_registry,
        _tool_catalog=VersionedToolRegistry(),
        _working_set=ToolWorkingSetManager(
            pinned_tools=["search_tools", "call_tool", "bash"]
        ),
        _source="cli",
        _user_id="root",
        _core_profile=profile,
        _session_id="sess-2",
    )
    pool._pool["sess-2"] = CoreEntry(agent=fake_agent, profile=profile)

    agent = await pool.acquire(
        "sess-2",
        source="feishu",
        user_id="ou_xxx",
        profile=None,
    )

    assert agent is fake_agent
    assert isinstance(fake_agent._tool_registry, VersionedToolRegistry)
    assert fake_agent._tool_catalog is fake_registry
    assert fake_agent._tool_registry.has("bash") is True
    assert fake_agent._tool_catalog.has("bash") is True
    assert fake_agent._source == "feishu"
    assert fake_agent._user_id == "ou_xxx"
    assert fake_agent._core_profile == profile
    assert pool._pool["sess-2"].profile == profile
    assert captured["profile"] == profile
    assert captured["memory_owner_id"] == "ou_xxx"


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
async def test_cancel_session_tasks_skips_still_queued_requests() -> None:
    """取消标记生效时，dispatch 应跳过仍在队列中的同 session 请求。"""
    routed: list[str] = []

    core_pool = MagicMock()
    core_pool.scan_expired = MagicMock(return_value=[])
    core_pool.restore_from_checkpoints = AsyncMock(return_value=0)
    core_pool.evict_all = AsyncMock(return_value=None)

    scheduler = KernelScheduler(kernel=MagicMock(), core_pool=core_pool)

    async def fast_route(request: KernelRequest) -> None:
        routed.append(request.text)
        await scheduler._out_bus.publish(
            request.session_id,
            request.request_id,
            AgentRunResult(output_text="ok"),
        )

    scheduler._run_and_route = fast_route  # type: ignore[method-assign]
    await scheduler.start()
    try:
        sid = "web:alice"
        req1 = KernelRequest.create(text="first", session_id=sid)
        handle1 = await scheduler.submit(req1)
        await asyncio.wait_for(scheduler.wait_result(handle1), timeout=5.0)
        assert routed == ["first"]

        scheduler._cancelled_sessions.add(sid)
        scheduler._track_enqueue(sid)
        queued = KernelRequest.create(text="queued-after-cancel", session_id=sid)
        await scheduler._queue.put(queued)

        await asyncio.sleep(0.05)
        assert routed == ["first"]
        assert sid not in scheduler._cancelled_sessions  # type: ignore[attr-defined]
    finally:
        await scheduler.stop()


def test_maybe_clear_cancelled_waits_for_queued_requests() -> None:
    """cancel 标记仅在 inflight 与排队深度均为 0 时清除。"""
    scheduler = KernelScheduler(kernel=MagicMock(), core_pool=MagicMock())
    sid = "web:alice"
    scheduler._cancelled_sessions.add(sid)
    scheduler._queued_by_session[sid] = 2

    scheduler._maybe_clear_cancelled(sid)
    assert sid in scheduler._cancelled_sessions

    scheduler._track_dequeue(sid)
    scheduler._maybe_clear_cancelled(sid)
    assert sid in scheduler._cancelled_sessions

    scheduler._track_dequeue(sid)
    scheduler._maybe_clear_cancelled(sid)
    assert sid not in scheduler._cancelled_sessions


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
    config.agent.working_set_size = 6
    config.agent.max_iterations = 10
    config.tools = MagicMock()
    config.tools.core_tools = [
        "search_tools",
        "call_tool",
        "bash",
        "request_permission",
        "ask_user",
    ]
    config.tools.pinned_tools = []
    config.tools.get_template.return_value = SimpleNamespace(
        exposure="pinned", extra=[]
    )
    config.memory = MagicMock()
    config.memory.enabled = False
    config.memory.max_working_tokens = 4000
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

    turn_id = await agent.prepare_turn("测试消息")

    assert turn_id == 1
    assert (
        recall_called
    ), "prepare_turn 在 memory_enabled=True 时应调用 recall_policy.should_recall"
    assert len(agent._context.get_messages()) == 1
    assert agent._context.messages[0]["content"].startswith("[Time:")
    assert "测试消息" in agent._context.messages[0]["content"]


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

    summary, kept = await kernel.compress_context(agent, keep_recent_turns=1)

    assert summary == ""
    assert kept == 4
    assert [m["role"] for m in ctx.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert ctx.messages[0]["content"] == "u2"


@pytest.mark.asyncio
async def test_compress_context_inserts_single_summary_message_before_kept_turns() -> (
    None
):
    """有摘要时：messages 仅保留一条摘要 user + keep_recent 段。"""
    registry = VersionedToolRegistry()
    kernel = AgentKernel(tool_registry=registry)

    ctx = ConversationContext()
    ctx.messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    wm = SimpleNamespace(running_summary=None, compression_round=0)
    llm = AsyncMock()
    llm.chat = AsyncMock(return_value=SimpleNamespace(content="  压缩结果  "))
    agent = SimpleNamespace(
        _context=ctx,
        _summary_llm_client=llm,
        _working_memory=wm,
        _build_system_prompt=MagicMock(return_value="主系统提示"),
    )

    summary, kept = await kernel.compress_context(agent, keep_recent_turns=1)

    assert summary.strip() == "压缩结果"
    assert kept == 3
    assert len(ctx.messages) == 3
    assert ctx.messages[0]["role"] == "user"
    assert "[会话进行中摘要]" in ctx.messages[0]["content"]
    assert "压缩结果" in ctx.messages[0]["content"]
    assert ctx.messages[1]["content"] == "u2"
    assert ctx.messages[2]["role"] == "assistant"
    assert wm.running_summary == "压缩结果"
    assert wm.compression_round == 1


@pytest.mark.asyncio
async def test_summarize_messages_passes_transcript_and_appends_summarize() -> None:
    """待折叠段浅拷贝进 chat，末尾追加请总结；system 与主 Agent 完全一致。"""
    registry = VersionedToolRegistry()
    kernel = AgentKernel(tool_registry=registry)

    llm = AsyncMock()
    llm.chat = AsyncMock(return_value=SimpleNamespace(content="合并后的记忆"))
    agent = SimpleNamespace(
        _summary_llm_client=llm,
        _build_system_prompt=MagicMock(return_value="主系统提示"),
    )
    old_messages = [
        {"role": "user", "content": "新问题"},
        {"role": "assistant", "content": "新回答"},
    ]

    out = await kernel._summarize_messages(agent, old_messages)

    assert out == "合并后的记忆"
    llm.chat.assert_called_once()
    call_kw = llm.chat.call_args
    msgs = call_kw[1]["messages"]
    assert msgs[0]["content"] == "新问题"
    assert msgs[1]["content"] == "新回答"
    assert msgs[0] is not old_messages[0]
    assert msgs[-1]["role"] == "user"
    assert "请总结" in msgs[-1]["content"]
    assert "工具结果" in msgs[-1]["content"]
    sys_msg = call_kw[1]["system_message"]
    assert sys_msg == "主系统提示"


@pytest.mark.asyncio
async def test_session_summarizer_uses_original_messages_and_normal_system_prompt() -> (
    None
):
    llm = AsyncMock()
    llm.chat = AsyncMock(return_value=SimpleNamespace(content="长期摘要"))
    summarizer = SessionSummarizer(llm_client=llm)
    stats = CoreStatsAction(
        session_id="feishu:user:ou_1",
        turn_count=2,
        token_usage={"total_tokens": 123},
    )
    messages = [
        {"role": "user", "content": "用户目标"},
        {"role": "assistant", "content": "助手回答"},
        {"role": "tool", "content": "工具结果"},
    ]

    out = await summarizer._generate_summary(
        stats,
        messages,
        system_message="普通对话 system",
    )

    assert out == "长期摘要"
    llm.chat.assert_awaited_once()
    call_kw = llm.chat.call_args[1]
    assert call_kw["system_message"] == "普通对话 system"
    sent = call_kw["messages"]
    assert sent[:3] == messages
    assert sent[0] is not messages[0]
    assert sent[-1]["role"] == "user"
    assert "Please summarize everything above" in sent[-1]["content"]
    assert "请总结上文内容" in sent[-1]["content"]


@pytest.mark.asyncio
async def test_kernel_run_records_tool_names_called_in_metadata() -> None:
    """AgentKernel.run 在本轮实际 execute 过的工具名写入 metadata["_tool_names_called]。"""
    from agent_core.kernel_interface import ReturnAction, ToolCallAction
    from agent_core.tools.base import ToolResult

    async def fake_run_loop(turn_id: int = 0, hooks=None):
        await asyncio.sleep(0)
        yield ToolCallAction(
            tool_call_id="t1",
            tool_name="create_subagent",
            arguments="{}",
        )
        yield ReturnAction(message="完成")

    agent = MagicMock(
        spec=["run_loop", "_session_id", "_tool_registry", "_core_profile"]
    )
    agent.run_loop = fake_run_loop
    agent._session_id = "cli:test"
    agent._tool_registry = None
    agent._core_profile = None

    registry = MagicMock()
    registry.execute = AsyncMock(
        return_value=ToolResult(success=True, data={"subagent_id": "x"}, message="ok")
    )
    kernel = AgentKernel(tool_registry=registry)

    result = await kernel.run(agent, turn_id=1)
    assert result.output_text == "完成"
    assert result.metadata.get("_tool_names_called") == ["create_subagent"]
    registry.execute.assert_called_once()


@pytest.mark.asyncio
async def test_kernel_run_injects_configured_workspace_admin(tmp_path) -> None:
    """Kernel 工具执行上下文应使用 config 解析后的有效 workspace admin 状态。"""
    from agent_core.kernel_interface import ReturnAction, ToolCallAction
    from agent_core.tools.base import ToolResult

    cfg = Config(
        llm=LLMConfig(api_key="k", model="m"),
        command_tools=CommandToolsConfig(
            base_dir=str(tmp_path),
            workspace_base_dir=str(tmp_path / "workspace_parent"),
            workspace_isolation_enabled=True,
            bash_os_user_enabled=True,
            bash_os_user_home_base_dir=str(tmp_path / "homes"),
            workspace_admin_memory_owners=["feishu:u42"],
        ),
    )
    profile = CoreProfile.full_from_config(
        cfg,
        frontend_id="feishu",
        dialog_window_id="u42",
    )

    async def fake_run_loop(turn_id: int = 0, hooks=None):
        await asyncio.sleep(0)
        yield ToolCallAction(
            tool_call_id="t1",
            tool_name="tool_a",
            arguments="{}",
        )
        yield ReturnAction(message="完成")

    agent = SimpleNamespace(
        run_loop=fake_run_loop,
        _session_id="feishu:u42",
        _parent_session_id="",
        _tool_registry=None,
        _core_profile=profile,
        _config=cfg,
        _source="feishu",
        _user_id="u42",
    )
    registry = MagicMock()
    registry.execute = AsyncMock(return_value=ToolResult(success=True, message="ok"))
    kernel = AgentKernel(tool_registry=registry)

    result = await kernel.run(agent, turn_id=1)

    assert result.output_text == "完成"
    ctx = registry.execute.call_args.kwargs["__execution_context__"]
    assert ctx["bash_workspace_admin"] is True


@pytest.mark.asyncio
async def test_kernel_run_propagates_delegated_tool_name_from_call_tool() -> None:
    """call_tool 返回的 _delegated_tool_name 应被 kernel 追加到 _tool_names_called。"""
    from agent_core.kernel_interface import ReturnAction, ToolCallAction
    from agent_core.tools.base import ToolResult

    async def fake_run_loop(turn_id: int = 0, hooks=None):
        await asyncio.sleep(0)
        yield ToolCallAction(
            tool_call_id="t1",
            tool_name="call_tool",
            arguments='{"name": "create_parallel_subagents", "arguments": {}}',
        )
        yield ReturnAction(message="子任务已创建")

    agent = MagicMock(
        spec=["run_loop", "_session_id", "_tool_registry", "_core_profile"]
    )
    agent.run_loop = fake_run_loop
    agent._session_id = "shuiyuan:Osc7"
    agent._tool_registry = None
    agent._core_profile = None

    registry = MagicMock()
    registry.execute = AsyncMock(
        return_value=ToolResult(
            success=True,
            data={"ids": ["sub1", "sub2"]},
            message="ok",
            metadata={"_delegated_tool_name": "create_parallel_subagents"},
        )
    )
    kernel = AgentKernel(tool_registry=registry)

    result = await kernel.run(agent, turn_id=1)
    assert result.output_text == "子任务已创建"
    names = result.metadata.get("_tool_names_called")
    assert "call_tool" in names
    assert "create_parallel_subagents" in names


@pytest.mark.asyncio
async def test_kernel_run_no_metadata_when_no_tools_executed() -> None:
    from agent_core.kernel_interface import ReturnAction

    async def fake_run_loop(turn_id: int = 0, hooks=None):
        await asyncio.sleep(0)
        yield ReturnAction(message="仅文本")

    agent = MagicMock(spec=["run_loop", "_tool_registry", "_core_profile"])
    agent.run_loop = fake_run_loop
    agent._tool_registry = None
    agent._core_profile = None

    registry = MagicMock()
    kernel = AgentKernel(tool_registry=registry)

    result = await kernel.run(agent, turn_id=1)
    assert result.output_text == "仅文本"
    assert result.metadata == {}


@pytest.mark.asyncio
async def test_context_overflow_after_compress_emits_chat_history_summarized_trace() -> (
    None
):
    """上下文触顶压缩后应通过 on_trace_event 通知各前端（IPC trace / 飞书 / CLI）。"""
    from agent_core.interfaces import AgentHooks
    from agent_core.kernel_interface import (
        ContextCompressedEvent,
        ContextOverflowAction,
        ReturnAction,
    )

    traces: list[dict[str, Any]] = []

    async def on_trace(evt: dict[str, Any]) -> None:
        traces.append(dict(evt))

    class _OverflowAgent:
        _session_id = "cli:test-overflow"

        async def run_loop(self, turn_id: int = 0, hooks=None):
            received = yield ContextOverflowAction(
                current_tokens=900_000,
                threshold_tokens=800_000,
                session_id=self._session_id,
            )
            assert isinstance(received, ContextCompressedEvent)
            yield ReturnAction(message="done")

    registry = VersionedToolRegistry()
    kernel = AgentKernel(tool_registry=registry)
    hooks = AgentHooks(on_trace_event=on_trace)

    with patch.object(
        AgentKernel,
        "compress_context",
        new_callable=AsyncMock,
        return_value=("folded summary text", 5),
    ):
        result = await kernel.run(_OverflowAgent(), turn_id=2, hooks=hooks)

    assert result.output_text == "done"
    summarized = [t for t in traces if t.get("type") == "chat_history_summarized"]
    assert len(summarized) == 1
    ev = summarized[0]
    assert ev["message"] == "Chat History Summarized."
    assert ev["messages_kept"] == 5
    assert ev["current_tokens"] == 900_000
    assert ev["threshold_tokens"] == 800_000
    assert ev["had_summary"] is True
    assert ev["session_id"] == "cli:test-overflow"


def test_kernel_task_exception_detail_preserves_message() -> None:
    from system.kernel.scheduler import _kernel_task_exception_detail

    assert "boom" in _kernel_task_exception_detail(ValueError("boom"))


def test_kernel_task_exception_detail_empty_str_falls_back_to_typename() -> None:
    from system.kernel.scheduler import _kernel_task_exception_detail

    class Silent(RuntimeError):
        def __str__(self) -> str:
            return ""

    assert "Silent" in _kernel_task_exception_detail(Silent())


def test_kernel_task_exception_detail_read_timeout_named_no_message_gets_hint() -> None:
    """与某些 HTTP 客户端一致：异常类型为 ReadTimeout 但 str 为空时仍能提示。"""
    from system.kernel.scheduler import _kernel_task_exception_detail

    exc = type("ReadTimeout", (Exception,), {})()
    detail = _kernel_task_exception_detail(exc)
    assert detail.startswith("ReadTimeout")
    assert "超时" in detail


def test_kernel_task_exception_detail_httpx_read_timeout_includes_url() -> None:
    import httpx

    from system.kernel.scheduler import _kernel_task_exception_detail

    req = httpx.Request("GET", "https://api.example.com/v1/x")
    exc = httpx.ReadTimeout("timed out", request=req)
    detail = _kernel_task_exception_detail(exc)
    assert "timed out" in detail
    assert "GET https://api.example.com/v1/x" in detail
    assert "超时" in detail


def _minimal_scheduler_for_cancel_tests() -> KernelScheduler:
    core_pool = SimpleNamespace(
        acquire=AsyncMock(),
        get_live_entry=lambda _sid: None,
        touch=lambda _sid: None,
        flush_pending_subagent_lifecycle_for_parent=lambda _sid: None,
        restore_from_checkpoints=AsyncMock(return_value=0),
    )
    return KernelScheduler(
        kernel=SimpleNamespace(run=AsyncMock()),  # type: ignore[arg-type]
        core_pool=core_pool,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_inflight_decremented_when_cancelled_waiting_for_session_lock() -> None:
    """在 session 锁排队时被 cancel 必须回收 inflight，否则会永久拒收新消息。"""
    scheduler = _minimal_scheduler_for_cancel_tests()
    session_id = "feishu:user:test"
    lock = await scheduler._get_session_lock(session_id)
    await lock.acquire()

    req = KernelRequest.create(text="queued", session_id=session_id)

    async def _wait_for_lock() -> None:
        await scheduler._run_and_route(req)

    waiter = asyncio.create_task(_wait_for_lock())
    for _ in range(50):
        if scheduler.session_inflight_request_count(session_id) == 1:
            break
        await asyncio.sleep(0.01)
    assert scheduler.session_inflight_request_count(session_id) == 1

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert scheduler.session_inflight_request_count(session_id) == 0
    lock.release()


@pytest.mark.asyncio
async def test_cancel_clears_stale_flag_after_skip_when_no_inflight() -> None:
    """idle cancel 后 skip 型请求应清掉 cancel 标记，后续请求不再被拒。"""
    scheduler = _minimal_scheduler_for_cancel_tests()
    session_id = "feishu:user:test2"
    scheduler._cancelled_sessions.add(session_id)

    skipped = KernelRequest.create(text="skip-me", session_id=session_id)
    await scheduler._run_and_route(skipped)

    assert session_id not in scheduler._cancelled_sessions
    assert scheduler.session_inflight_request_count(session_id) == 0


@pytest.mark.asyncio
async def test_cancel_skip_aborts_agent_wake_delivery() -> None:
    """session cancel 跳过 wake inject 时应 abort，保留 wake 供后续 poll 重试。"""
    from agent_core.tools.agent_wake import (
        clear_all_wakes_for_tests,
        list_wakes,
        poll_due_wakes,
        register_wake,
    )

    clear_all_wakes_for_tests()
    scheduler = _minimal_scheduler_for_cancel_tests()
    session_id = "cli:root"
    wid = register_wake(
        session_id=session_id,
        fire_at=time.time() - 1,
        message="wake after cancel",
        wake_id="wake-cancel-skip",
    )
    wake = poll_due_wakes()[0]
    assert wake["wake_id"] == wid

    from agent_core.tools.agent_wake import deliver_wake_via_inject

    set_notify_dependencies = __import__(
        "agent_core.tools.bash_job_notify", fromlist=["set_notify_dependencies"]
    ).set_notify_dependencies
    set_notify_dependencies(scheduler=scheduler, core_pool=None)
    deliver_wake_via_inject(wake=wake, scheduler=scheduler)
    assert len(list_wakes()) == 1
    assert list_wakes()[0]["staged"] is True

    scheduler._cancelled_sessions.add(session_id)
    req = KernelRequest.create(
        text="wake after cancel",
        session_id=session_id,
        metadata={"_wake_id": wid},
    )
    await scheduler._run_and_route(req)

    assert len(list_wakes()) == 1
    assert list_wakes()[0]["staged"] is False
    assert len(poll_due_wakes()) == 1


@pytest.mark.asyncio
async def test_cancel_does_not_skip_bash_job_system_inject() -> None:
    """session cancel 后 bash_job 系统通知仍应投递（不应像用户 turn 一样被 skip）。"""
    from agent_core.tools.bash_job_notify import (
        clear_all_tracking_for_tests,
        poll_terminal_jobs,
        register_local_job,
        set_notify_dependencies,
        deliver_via_inject,
        format_notification,
    )

    clear_all_tracking_for_tests()
    scheduler = _minimal_scheduler_for_cancel_tests()
    session_id = "cli:root"
    scheduler._core_pool.acquire = AsyncMock(  # type: ignore[attr-defined]
        return_value=SimpleNamespace(
            prepare_turn=AsyncMock(return_value=1),
            _finalize_turn=AsyncMock(),
            _session_logger=None,
        )
    )

    register_local_job(
        session_id=session_id,
        job_id="job-cancel-exempt",
        command="echo done",
        cwd="/tmp",
        log_path="/tmp/job.log",
        workspace_root="/tmp",
    )
    notes = await poll_terminal_jobs(max_items=5)
    assert len(notes) == 1

    set_notify_dependencies(scheduler=scheduler, core_pool=None)
    deliver_via_inject(
        session_id=session_id,
        text=format_notification(notes[0]),
        note=notes[0],
    )

    scheduler._cancelled_sessions.add(session_id)
    req = KernelRequest.create(
        text=format_notification(notes[0]),
        session_id=session_id,
        frontend_id="bash_job",
        metadata={
            "_bash_job_notify": {"job_id": "job-cancel-exempt", "remote": False},
        },
    )
    await scheduler._run_and_route(req)

    scheduler._kernel.run.assert_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cancel_still_skips_user_turn_after_cancel() -> None:
    """cancel 豁免仅限系统 inject；普通用户 turn 仍应 skip。"""
    scheduler = _minimal_scheduler_for_cancel_tests()
    session_id = "cli:root"
    scheduler._cancelled_sessions.add(session_id)
    req = KernelRequest.create(text="user message", session_id=session_id)
    await scheduler._run_and_route(req)
    scheduler._kernel.run.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cancel_session_tasks_clears_mark_when_lock_waiter_aborted() -> None:
    """stop 时若第二个请求在等锁，cancel 后 session 应恢复可投递。"""
    scheduler = _minimal_scheduler_for_cancel_tests()
    session_id = "feishu:user:test3"

    hold = asyncio.Event()
    release = asyncio.Event()

    async def _slow_run(*_a: Any, **_k: Any) -> Any:
        from agent_core.interfaces import AgentRunResult

        hold.set()
        await release.wait()
        return AgentRunResult(output_text="done")

    scheduler._kernel.run = AsyncMock(side_effect=_slow_run)  # type: ignore[attr-defined]
    scheduler._core_pool.acquire = AsyncMock(  # type: ignore[attr-defined]
        return_value=SimpleNamespace(
            prepare_turn=AsyncMock(return_value=1),
            _finalize_turn=AsyncMock(),
            _session_logger=None,
        )
    )

    await scheduler.start()
    req1 = KernelRequest.create(text="first", session_id=session_id)
    req2 = KernelRequest.create(text="second", session_id=session_id)
    await scheduler.submit(req1)
    for _ in range(100):
        if hold.is_set():
            break
        await asyncio.sleep(0.01)
    await scheduler.submit(req2)
    await asyncio.sleep(0.05)
    assert scheduler.session_inflight_request_count(session_id) >= 1

    await scheduler.cancel_session_tasks(session_id)
    release.set()

    assert scheduler.session_inflight_request_count(session_id) == 0
    assert session_id not in scheduler._cancelled_sessions

    req3 = KernelRequest.create(text="third", session_id=session_id)
    h3 = await scheduler.submit(req3)
    result = await asyncio.wait_for(scheduler.wait_result(h3), timeout=5.0)
    assert result.output_text == "done"

    await scheduler.stop()
