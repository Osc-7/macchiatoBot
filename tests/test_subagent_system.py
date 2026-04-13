"""测试：异步 Multi-Agent 通信系统（CorePool 统一进程表版本）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_pool():
    from system.kernel import CorePool

    pool = CorePool()
    scheduler = MagicMock()
    scheduler.inject_turn = MagicMock()
    scheduler.cancel_session_tasks = MagicMock(return_value=True)
    pool.set_scheduler(scheduler)
    return pool, scheduler


def _exec_cli_root() -> dict:
    return {"session_id": "cli:root"}


def _seed_parent_session(pool, session_id: str = "cli:root") -> None:
    """为 list_agents / 父权校验测试在进程表中放入父会话占位条目。"""
    from agent_core.kernel_interface import CoreProfile
    from system.kernel.core_pool import CoreEntry

    prof = CoreProfile.default_full(frontend_id="cli", dialog_window_id="root")
    pool._pool[session_id] = CoreEntry(agent=None, profile=prof)


class TestAgentMessage:
    def test_basic_fields(self):
        from agent_core.kernel_interface.action import AgentMessage

        msg = AgentMessage(
            message_id="id-001",
            sender_session="cli:root",
            receiver_session="sub:abc123",
            message_type="task",
            subagent_id="abc123",
        )
        assert msg.message_id == "id-001"
        assert msg.sender_session == "cli:root"
        assert msg.receiver_session == "sub:abc123"
        assert msg.message_type == "task"
        assert msg.subagent_id == "abc123"


class TestCorePoolSubagentLifecycle:
    def test_register_and_get(self):
        pool, _ = _make_pool()
        entry = pool.register_sub(
            sub_session_id="sub:sub-001",
            parent_session_id="cli:root",
            task_description="Test task",
        )
        assert pool.get_sub_info("sub:sub-001") is entry
        assert pool.get_sub_info("sub:ghost") is None

    def test_list_by_parent(self):
        pool, _ = _make_pool()
        pool.register_sub(
            sub_session_id="sub:sub-001",
            parent_session_id="cli:root",
            task_description="Task 1",
        )
        pool.register_sub(
            sub_session_id="sub:sub-002",
            parent_session_id="cli:root",
            task_description="Task 2",
        )
        pool.register_sub(
            sub_session_id="sub:sub-003",
            parent_session_id="feishu:u1",
            task_description="Task 3",
        )
        children = pool.list_subs_by_parent("cli:root")
        assert len(children) == 2

    def test_scan_stale_subagent_zombies(self):
        import time

        pool, _ = _make_pool()
        entry = pool.register_sub(
            sub_session_id="sub:stale-1",
            parent_session_id="cli:root",
            task_description="z",
        )
        entry.sub_status = "completed"
        entry.sub_result = "ok"
        entry.sub_completed_at = time.time() - 4000.0
        pool._pool.pop("sub:stale-1", None)
        pool._zombies["sub:stale-1"] = entry
        stale = pool.scan_stale_subagent_zombies(3600.0)
        assert "sub:stale-1" in stale
        assert pool.scan_stale_subagent_zombies(86400.0 * 365) == []

    def test_on_complete_updates_status_and_injects(self):
        pool, scheduler = _make_pool()
        entry = pool.register_sub(
            sub_session_id="sub:sub-001",
            parent_session_id="cli:root",
            task_description="Test task",
        )
        pool.on_sub_complete("sub:sub-001", "任务完成，结果是 42")
        assert entry.sub_status == "completed"
        assert entry.sub_result == "任务完成，结果是 42"
        assert entry.sub_completed_at is not None
        scheduler.inject_turn.assert_called_once()
        request = scheduler.inject_turn.call_args[0][0]
        assert request.session_id == "cli:root"
        assert "任务完成" in request.text

    def test_on_fail_updates_status_and_injects(self):
        pool, scheduler = _make_pool()
        entry = pool.register_sub(
            sub_session_id="sub:sub-002",
            parent_session_id="cli:root",
            task_description="Test task",
        )
        pool.on_sub_fail("sub:sub-002", "连接超时")
        assert entry.sub_status == "failed"
        assert entry.sub_error == "连接超时"
        scheduler.inject_turn.assert_called_once()
        request = scheduler.inject_turn.call_args[0][0]
        assert "连接超时" in request.text

    def test_cancel_running(self):
        pool, scheduler = _make_pool()
        entry = pool.register_sub(
            sub_session_id="sub:sub-003",
            parent_session_id="cli:root",
            task_description="Test task",
        )
        entry.bg_task = MagicMock()
        entry.bg_task.done.return_value = False
        assert pool.cancel_sub("sub:sub-003") is True
        assert entry.sub_status == "cancelled"
        entry.bg_task.cancel.assert_called_once()
        scheduler.cancel_session_tasks.assert_called_once_with("sub:sub-003")

    def test_on_complete_ignored_after_cancel(self):
        pool, scheduler = _make_pool()
        entry = pool.register_sub(
            sub_session_id="sub:sub-004",
            parent_session_id="cli:root",
            task_description="Test task",
        )
        entry.sub_status = "cancelled"
        pool.on_sub_complete("sub:sub-004", "late result")
        scheduler.inject_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_evict_completed_subagent_becomes_zombie(self):
        from agent_core.kernel_interface import CoreProfile
        from system.kernel import CoreEntry

        pool, _ = _make_pool()
        profile = CoreProfile.default_sub(
            allowed_tools=None,
            frontend_id="subagent",
            dialog_window_id="sub-005",
        )
        entry = CoreEntry(
            agent=MagicMock(),
            profile=profile,
            parent_session_id="cli:root",
            task_description="done task",
            sub_status="completed",
            sub_result="ok",
        )
        pool._pool["sub:sub-005"] = entry
        pool._kernel = MagicMock()
        pool._kernel.kill = AsyncMock(return_value=None)
        await pool.evict("sub:sub-005")
        zombie = pool.get_sub_info("sub:sub-005")
        assert zombie is not None
        assert zombie.agent is None
        assert pool.is_zombie("sub:sub-005") is True

    @pytest.mark.asyncio
    async def test_subagent_zombie_visible_while_evict_awaits_teardown(self):
        """evict 在 await kill/summarize/close 期间，zombie 应已可查，避免父会话拉取 NOT_FOUND。"""
        from agent_core.kernel_interface import CoreProfile
        from system.kernel import CoreEntry

        pool, _ = _make_pool()
        profile = CoreProfile.default_sub(
            allowed_tools=None,
            frontend_id="subagent",
            dialog_window_id="race-001",
        )
        entry = CoreEntry(
            agent=MagicMock(),
            profile=profile,
            parent_session_id="cli:root",
            task_description="done",
            sub_status="completed",
            sub_result="full report",
        )
        pool._pool["sub:race-001"] = entry

        resume_kill = asyncio.Event()

        async def slow_kill(agent):
            await resume_kill.wait()
            return MagicMock()

        pool._kernel = MagicMock()
        pool._kernel.kill = slow_kill
        pool._summarizer = MagicMock()
        pool._summarizer.summarize_and_persist = AsyncMock()

        ev_task = asyncio.create_task(pool.evict("sub:race-001"))
        await asyncio.sleep(0)
        # 已 pop 且 kill 在等 resume_kill；此时 zombie 应已登记
        mid = pool.get_sub_info("sub:race-001")
        assert mid is not None, "zombie must exist during async evict teardown"
        assert mid.sub_status == "completed"
        assert mid.sub_result == "full report"
        assert pool.is_zombie("sub:race-001")

        resume_kill.set()
        await ev_task
        final = pool.get_sub_info("sub:race-001")
        assert final is not None
        assert final.agent is None

    @pytest.mark.asyncio
    async def test_acquire_rehydrate_subagent_merges_zombie_sub_status(self):
        """子任务 evict 入 zombie 后再次 acquire 须保留 completed，避免 get_subagent_status 误报 running。"""
        from agent_core.kernel_interface import CoreProfile
        from system.kernel import CoreEntry

        pool, _ = _make_pool()
        profile = CoreProfile.default_sub(
            allowed_tools=None,
            frontend_id="subagent",
            dialog_window_id="rehydrate-001",
        )
        entry = CoreEntry(
            agent=MagicMock(),
            profile=profile,
            parent_session_id="cli:root",
            task_description="t",
            sub_status="completed",
            sub_result="final answer",
            sub_completed_at=1700000000.0,
        )
        pool._pool["sub:rehydrate-001"] = entry
        pool._kernel = MagicMock()
        pool._kernel.kill = AsyncMock(return_value=None)
        await pool.evict("sub:rehydrate-001")
        assert pool.get_sub_info("sub:rehydrate-001").sub_status == "completed"

        fake_agent = MagicMock()
        fake_agent._checkpoint_ttl_offset = 0.0

        async def fake_load(sid: str, **kwargs: object):
            return fake_agent, profile, None

        pool._load = fake_load  # type: ignore[method-assign]

        out = await pool.acquire("sub:rehydrate-001")
        assert out is fake_agent
        live = pool.get_live_entry("sub:rehydrate-001")
        assert live is not None
        assert live.sub_status == "completed"
        assert live.sub_result == "final answer"
        assert live.sub_completed_at == 1700000000.0
        assert pool.is_zombie("sub:rehydrate-001") is False


class TestInjectTurn:
    def test_has_waiter_returns_false_for_unregistered_request(self):
        from system.kernel.scheduler import OutputBus

        bus = OutputBus()
        assert bus.has_waiter("some-random-id") is False

    def test_has_waiter_returns_true_after_register(self):
        from system.kernel.scheduler import OutputBus

        loop = asyncio.new_event_loop()
        try:
            async def _run():
                bus = OutputBus()
                bus.register_waiter("req-001")
                assert bus.has_waiter("req-001") is True
                assert bus.has_waiter("req-002") is False

            loop.run_until_complete(_run())
        finally:
            loop.close()

    def test_inject_turn_puts_request_in_queue_without_waiter(self):
        from agent_core.kernel_interface import KernelRequest
        from system.kernel.scheduler import KernelScheduler

        loop = asyncio.new_event_loop()
        try:
            async def _run():
                scheduler = KernelScheduler(kernel=MagicMock(), core_pool=MagicMock())
                request = KernelRequest.create(
                    text="hello from subagent",
                    session_id="cli:root",
                    frontend_id="subagent",
                    priority=-1,
                )
                scheduler.inject_turn(request)
                assert scheduler.queue_size == 1
                assert not scheduler._out_bus.has_waiter(request.request_id)

            loop.run_until_complete(_run())
        finally:
            loop.close()


class TestSendMessageToAgentTool:
    @pytest.mark.asyncio
    async def test_send_message_success(self):
        from system.tools.subagent_tools import SendMessageToAgentTool

        mock_scheduler = MagicMock()
        mock_scheduler.inject_turn = MagicMock()
        mock_scheduler.register_p2p_reply_waiter = MagicMock()
        tool = SendMessageToAgentTool(scheduler=mock_scheduler)
        result = await tool.execute(
            session_id="shuiyuan:Osc7",
            content="Hello from test",
            require_reply=False,
            __execution_context__={"session_id": "cli:root"},
        )
        assert result.success is True
        assert result.data["target_session"] == "shuiyuan:Osc7"
        mock_scheduler.inject_turn.assert_called_once()
        mock_scheduler.register_p2p_reply_waiter.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_message_require_reply_blocks_until_complete(self):
        from system.kernel.scheduler import KernelScheduler
        from system.tools.subagent_tools import SendMessageToAgentTool

        core_pool = MagicMock()
        core_pool.set_scheduler = MagicMock()
        sched = KernelScheduler(kernel=MagicMock(), core_pool=core_pool)
        mids: list[str] = []

        def fake_inject(req: object) -> None:
            from agent_core.kernel_interface.action import AgentMessage

            meta = getattr(req, "metadata", None) or {}
            am = meta.get("_agent_message")
            if isinstance(am, AgentMessage):
                mids.append(am.message_id)

        sched.inject_turn = fake_inject  # type: ignore[method-assign]

        tool = SendMessageToAgentTool(scheduler=sched)

        async def run_send() -> object:
            return await tool.execute(
                session_id="cli:b",
                content="question",
                require_reply=True,
                __execution_context__={"session_id": "cli:a"},
            )

        task = asyncio.create_task(run_send())
        await asyncio.sleep(0)
        assert len(mids) == 1
        assert sched.complete_p2p_reply(mids[0], "answer body") is True
        result = await task
        assert result.success is True
        assert result.data["reply_content"] == "answer body"

    @pytest.mark.asyncio
    async def test_send_message_rejected_when_sender_cancelled(self):
        from agent_core.kernel_interface import CoreProfile
        from system.tools import build_tool_registry

        pool, scheduler = _make_pool()
        pool.register_sub(
            sub_session_id="sub:abc123",
            parent_session_id="cli:root",
            task_description="test",
        ).sub_status = "cancelled"
        reg = build_tool_registry(
            profile=CoreProfile(mode="full"),
            core_pool=pool,
        )
        tool = reg.get("send_message_to_agent")
        assert tool is not None
        result = await tool.execute(
            session_id="cli:root",
            content="尝试发送",
            __execution_context__={"session_id": "sub:abc123"},
        )
        assert result.success is False
        assert result.error == "SUBAGENT_CANCELLED"
        scheduler.inject_turn.assert_not_called()


class TestReplyToMessageTool:
    @pytest.mark.asyncio
    async def test_reply_success(self):
        from agent_core.kernel_interface.action import AgentMessage
        from system.tools.subagent_tools import ReplyToMessageTool

        captured = []
        mock_scheduler = MagicMock()
        mock_scheduler.inject_turn = lambda req: captured.append(req)
        mock_scheduler.complete_p2p_reply = MagicMock(return_value=True)
        mock_scheduler.has_p2p_reply_waiter = MagicMock(return_value=False)
        tool = ReplyToMessageTool(scheduler=mock_scheduler)
        result = await tool.execute(
            correlation_id="msg-001",
            sender_session_id="cli:root",
            content="回复内容",
            __execution_context__={"session_id": "shuiyuan:Osc7"},
        )
        assert result.success is True
        req = captured[0]
        agent_msg: AgentMessage = req.metadata["_agent_message"]
        assert agent_msg.message_type == "reply"
        assert agent_msg.correlation_id == "msg-001"
        mock_scheduler.complete_p2p_reply.assert_called_once_with("msg-001", "回复内容")

    @pytest.mark.asyncio
    async def test_reply_skips_inject_when_blocking_waiter(self):
        from system.tools.subagent_tools import ReplyToMessageTool

        mock_scheduler = MagicMock()
        mock_scheduler.inject_turn = MagicMock()
        mock_scheduler.has_p2p_reply_waiter = MagicMock(return_value=True)
        mock_scheduler.complete_p2p_reply = MagicMock(return_value=True)
        tool = ReplyToMessageTool(scheduler=mock_scheduler)
        result = await tool.execute(
            correlation_id="msg-wait",
            sender_session_id="cli:root",
            content="仅唤醒",
            __execution_context__={"session_id": "sub:other"},
        )
        assert result.success is True
        mock_scheduler.inject_turn.assert_not_called()
        mock_scheduler.complete_p2p_reply.assert_called_once_with("msg-wait", "仅唤醒")


class TestToolRegistration:
    def test_no_core_pool_no_subagent_tools(self):
        from agent_core.kernel_interface import CoreProfile
        from system.tools import build_tool_registry

        reg = build_tool_registry(profile=CoreProfile(mode="full"))
        for tool_name in [
            "create_subagent",
            "create_parallel_subagents",
            "send_message_to_agent",
            "reply_to_message",
            "get_subagent_status",
            "reap_subagent",
            "cancel_subagent",
            "list_agents",
        ]:
            assert not reg.has(tool_name)

    def test_full_mode_with_core_pool(self):
        from agent_core.kernel_interface import CoreProfile
        from system.tools import build_tool_registry

        pool, _ = _make_pool()
        reg = build_tool_registry(profile=CoreProfile(mode="full"), core_pool=pool)
        assert reg.has("create_subagent")
        assert reg.has("create_parallel_subagents")
        assert reg.has("send_message_to_agent")
        assert reg.has("reply_to_message")
        assert reg.has("get_subagent_status")
        assert reg.has("reap_subagent")
        assert reg.has("cancel_subagent")
        assert reg.has("list_agents")

    def test_sub_mode_only_has_communication_tools(self):
        from agent_core.kernel_interface import CoreProfile
        from system.tools import build_tool_registry

        pool, _ = _make_pool()
        reg = build_tool_registry(profile=CoreProfile(mode="sub"), core_pool=pool)
        assert reg.has("send_message_to_agent")
        assert reg.has("reply_to_message")
        assert reg.has("list_agents")
        assert not reg.has("create_subagent")
        assert not reg.has("create_parallel_subagents")
        assert not reg.has("get_subagent_status")
        assert not reg.has("reap_subagent")
        assert not reg.has("cancel_subagent")


class TestListAgentsTool:
    @pytest.mark.asyncio
    async def test_my_children_filter(self):
        from system.tools.subagent_tools import ListAgentsTool

        pool, _ = _make_pool()
        _seed_parent_session(pool, "cli:root")
        pool.register_sub(
            sub_session_id="sub:c1",
            parent_session_id="cli:root",
            task_description="t1",
        )
        pool.register_sub(
            sub_session_id="sub:c2",
            parent_session_id="feishu:u1",
            task_description="t2",
        )
        tool = ListAgentsTool(core_pool=pool)
        r = await tool.execute(
            scope="my_children", __execution_context__=_exec_cli_root()
        )
        assert r.success
        assert r.data["count"] == 1
        assert r.data["agents"][0]["session_id"] == "sub:c1"

    @pytest.mark.asyncio
    async def test_parent_guard_get_foreign_sub(self):
        from system.tools.subagent_tools import GetSubagentStatusTool

        pool, _ = _make_pool()
        pool.register_sub(
            sub_session_id="sub:orphan",
            parent_session_id="cli:root",
            task_description="x",
        )
        tool = GetSubagentStatusTool(core_pool=pool)
        r = await tool.execute(
            subagent_id="orphan",
            __execution_context__={"session_id": "feishu:intruder"},
        )
        assert r.success is False
        assert r.error == "FORBIDDEN_NOT_YOUR_SUB"


class TestGetSubagentStatusTool:
    @pytest.mark.asyncio
    async def test_get_status_running(self):
        from system.tools.subagent_tools import GetSubagentStatusTool

        pool, _ = _make_pool()
        pool.register_sub(
            sub_session_id="sub:abc123",
            parent_session_id="cli:root",
            task_description="test task",
        )
        tool = GetSubagentStatusTool(core_pool=pool)
        result = await tool.execute(
            subagent_id="abc123", __execution_context__=_exec_cli_root()
        )
        assert result.success
        assert result.data["status"] == "running"

    @pytest.mark.asyncio
    async def test_get_status_completed_then_reap_removes_zombie(self):
        from system.tools.subagent_tools import GetSubagentStatusTool, ReapSubagentTool

        pool, _ = _make_pool()
        entry = pool.register_sub(
            sub_session_id="sub:xyz789",
            parent_session_id="cli:root",
            task_description="done task",
        )
        entry.sub_status = "completed"
        entry.sub_result = "report content"
        pool._zombies["sub:xyz789"] = entry
        pool._pool.pop("sub:xyz789", None)

        get_tool = GetSubagentStatusTool(core_pool=pool)
        ctx = _exec_cli_root()
        result = await get_tool.execute(subagent_id="xyz789", __execution_context__=ctx)
        assert result.success
        assert "report content" in result.data.get("result_preview", "")

        result_full = await get_tool.execute(
            subagent_id="xyz789", include_full_result=True, __execution_context__=ctx
        )
        assert result_full.success
        assert result_full.data["result"] == "report content"
        assert pool.get_sub_info("sub:xyz789") is not None

        reap_tool = ReapSubagentTool(core_pool=pool)
        reap_result = await reap_tool.execute(subagent_id="xyz789", __execution_context__=ctx)
        assert reap_result.success
        assert reap_result.data["result"] == "report content"
        assert pool.get_sub_info("sub:xyz789") is None

        reap_again = await reap_tool.execute(subagent_id="xyz789", __execution_context__=ctx)
        assert reap_again.success is True
        assert reap_again.data.get("already_reaped") is True

    @pytest.mark.asyncio
    async def test_reap_unknown_subagent_id_not_found(self):
        from system.tools.subagent_tools import ReapSubagentTool

        pool, _ = _make_pool()
        tool = ReapSubagentTool(core_pool=pool)
        r = await tool.execute(
            subagent_id="definitely-not-created", __execution_context__=_exec_cli_root()
        )
        assert r.success is False
        assert r.error == "SUBAGENT_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_reap_subagent_still_running(self):
        from system.tools.subagent_tools import ReapSubagentTool

        pool, _ = _make_pool()
        pool.register_sub(
            sub_session_id="sub:run001",
            parent_session_id="cli:root",
            task_description="running",
        )
        tool = ReapSubagentTool(core_pool=pool)
        result = await tool.execute(
            subagent_id="run001", __execution_context__=_exec_cli_root()
        )
        assert result.success is False
        assert result.error == "SUBAGENT_STILL_RUNNING"


class TestCancelSubagentTool:
    @pytest.mark.asyncio
    async def test_cancel_existing_running(self):
        from system.tools.subagent_tools import CancelSubagentTool

        pool, _ = _make_pool()
        pool.register_sub(
            sub_session_id="sub:abc123",
            parent_session_id="cli:root",
            task_description="test",
        )
        tool = CancelSubagentTool(core_pool=pool)
        result = await tool.execute(
            subagent_id="abc123", __execution_context__=_exec_cli_root()
        )
        assert result.success is True
        assert pool.get_sub_info("sub:abc123").sub_status == "cancelled"


class TestMergeAllowedTools:
    def test_merge_adds_communication_tools(self):
        from system.tools.subagent_tools import _merge_allowed_tools_for_subagent

        merged = _merge_allowed_tools_for_subagent(["read_file", "search_tools"])
        assert "send_message_to_agent" in merged
        assert "reply_to_message" in merged


class TestCreateSubagentTool:
    @pytest.mark.asyncio
    async def test_create_subagent_registers_entry(self):
        from system.tools.subagent_tools import CreateSubagentTool

        pool, scheduler = _make_pool()
        scheduler.submit = AsyncMock(return_value="req-1")
        scheduler.wait_result = AsyncMock(return_value=SimpleNamespace(output_text="done"))
        tool = CreateSubagentTool(core_pool=pool, scheduler=scheduler)
        result = await tool.execute(
            task="分析报告",
            __execution_context__={"session_id": "cli:root"},
        )
        assert result.success is True
        await asyncio.sleep(0)
        subagent_id = result.data["subagent_id"]
        assert pool.get_sub_info(f"sub:{subagent_id}") is not None


class TestCreateParallelSubagentsTool:
    @pytest.mark.asyncio
    async def test_parallel_creates_multiple(self):
        from system.tools.subagent_tools import CreateParallelSubagentsTool

        pool, scheduler = _make_pool()
        scheduler.submit = AsyncMock(return_value="req-1")
        scheduler.wait_result = AsyncMock(return_value=SimpleNamespace(output_text="done"))
        tool = CreateParallelSubagentsTool(core_pool=pool, scheduler=scheduler)
        result = await tool.execute(
            tasks=[{"task": "任务 A"}, {"task": "任务 B"}, {"task": "任务 C"}],
            __execution_context__={"session_id": "cli:root"},
        )
        assert result.success is True
        assert result.data["count"] == 3


class TestReapSubagentWorkspace:
    def test_remove_subagent_workspace_trees_deletes_data_and_tmp(self, tmp_path, monkeypatch):
        import agent_core.agent.workspace_paths as wp

        monkeypatch.setattr(wp, "_TMP_BASE_DIR", tmp_path / "mtmp")
        from agent_core.config import CommandToolsConfig
        from agent_core.agent.workspace_paths import (
            ensure_workspace_owner_layout,
            remove_subagent_workspace_trees,
        )

        cmd = CommandToolsConfig(workspace_base_dir=str(tmp_path / "ws"))
        ensure_workspace_owner_layout(cmd, "ab-cd-ef", source="subagent")
        assert (tmp_path / "ws" / "subagent" / "ab-cd-ef").is_dir()
        assert (tmp_path / "mtmp" / "subagent" / "ab-cd-ef").is_dir()
        (tmp_path / "ws" / "subagent" / "ab-cd-ef" / "f.txt").write_text("x", encoding="utf-8")
        remove_subagent_workspace_trees(cmd, "sub:ab-cd-ef")
        assert not (tmp_path / "ws" / "subagent" / "ab-cd-ef").exists()
        assert not (tmp_path / "mtmp" / "subagent" / "ab-cd-ef").exists()

    def test_reap_zombie_removes_workspace_dirs(self, tmp_path, monkeypatch):
        import agent_core.agent.workspace_paths as wp

        monkeypatch.setattr(wp, "_TMP_BASE_DIR", tmp_path / "mtmp")
        from agent_core.config import get_config
        from agent_core.agent.workspace_paths import ensure_workspace_owner_layout
        from system.kernel import CorePool

        cfg = get_config().model_copy(
            update={
                "command_tools": get_config().command_tools.model_copy(
                    update={"workspace_base_dir": str(tmp_path / "ws")}
                )
            }
        )
        pool = CorePool(config=cfg)
        ensure_workspace_owner_layout(cfg.command_tools, "zid01", source="subagent")
        pool._zombies["sub:zid01"] = object()
        pool.reap_zombie("sub:zid01")
        assert "sub:zid01" not in pool._zombies
        assert not (tmp_path / "ws" / "subagent" / "zid01").exists()
        assert not (tmp_path / "mtmp" / "subagent" / "zid01").exists()
