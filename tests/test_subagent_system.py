"""
测试：异步 Multi-Agent 通信系统

覆盖范围：
1. AgentMessage 协议结构
2. SubagentRegistry 状态流转（on_complete / on_fail / cancel）
3. first-done 语义：subagent 完成后 inject_turn 唤醒父 session
4. P2P 消息：send_message_to_agent + reply_to_message
5. inject_turn + OutputBus.has_waiter
6. 工具注册：full/sub 模式差异
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# AgentMessage 协议
# ---------------------------------------------------------------------------


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
        assert msg.require_reply is False
        assert msg.correlation_id is None

    def test_reply_message(self):
        from agent_core.kernel_interface.action import AgentMessage

        reply = AgentMessage(
            message_id="id-002",
            sender_session="sub:abc123",
            receiver_session="cli:root",
            message_type="reply",
            correlation_id="id-001",
        )
        assert reply.message_type == "reply"
        assert reply.correlation_id == "id-001"

    def test_query_with_require_reply(self):
        from agent_core.kernel_interface.action import AgentMessage

        q = AgentMessage(
            message_id="id-003",
            sender_session="cli:root",
            receiver_session="shuiyuan:Osc7",
            message_type="query",
            require_reply=True,
        )
        assert q.require_reply is True

    def test_exported_from_kernel_interface(self):
        from agent_core.kernel_interface import AgentMessage  # noqa: F401
        from system.kernel import AgentKernel  # noqa: F401 (kernel exports check)
        # AgentMessage 不在 system.kernel 里导出，从 agent_core.kernel_interface 导入即可
        assert AgentMessage is not None


# ---------------------------------------------------------------------------
# SubagentRegistry
# ---------------------------------------------------------------------------


class TestSubagentRegistry:
    def _make_registry(self):
        from system.kernel.subagent_registry import SubagentRegistry

        registry = SubagentRegistry()
        mock_scheduler = MagicMock()
        mock_scheduler.inject_turn = MagicMock()
        registry.set_scheduler(mock_scheduler)
        return registry, mock_scheduler

    def _make_info(self, subagent_id: str, parent: str = "cli:root"):
        from system.kernel.subagent_registry import SubagentInfo

        return SubagentInfo(
            subagent_id=subagent_id,
            parent_session_id=parent,
            task_description="Test task",
        )

    def test_register_and_get(self):
        registry, _ = self._make_registry()
        info = self._make_info("sub-001")
        registry.register(info)
        assert registry.get("sub-001") is info
        assert registry.get("nonexistent") is None

    def test_list_by_parent(self):
        registry, _ = self._make_registry()
        info1 = self._make_info("sub-001", parent="cli:root")
        info2 = self._make_info("sub-002", parent="cli:root")
        info3 = self._make_info("sub-003", parent="shuiyuan:Osc7")
        registry.register(info1)
        registry.register(info2)
        registry.register(info3)

        children = registry.list_by_parent("cli:root")
        assert len(children) == 2
        assert info1 in children
        assert info2 in children

    def test_on_complete_updates_status_and_injects(self):
        registry, mock_scheduler = self._make_registry()
        info = self._make_info("sub-001")
        registry.register(info)

        registry.on_complete("sub-001", "任务完成，结果是 42")

        assert info.status == "completed"
        assert info.result == "任务完成，结果是 42"
        assert info.completed_at is not None
        # inject_turn 应该被调用一次（first-done 唤醒父 session）
        mock_scheduler.inject_turn.assert_called_once()

    def test_on_complete_inject_turn_content(self):
        registry, mock_scheduler = self._make_registry()
        info = self._make_info("sub-001", parent="cli:root")
        registry.register(info)

        registry.on_complete("sub-001", "result text")

        call_args = mock_scheduler.inject_turn.call_args
        request = call_args[0][0]
        assert request.session_id == "cli:root"
        assert "result text" in request.text
        assert request.metadata.get("_agent_message") is not None

    def test_on_fail_updates_status_and_injects(self):
        registry, mock_scheduler = self._make_registry()
        info = self._make_info("sub-002")
        registry.register(info)

        registry.on_fail("sub-002", "连接超时")

        assert info.status == "failed"
        assert info.error == "连接超时"
        mock_scheduler.inject_turn.assert_called_once()

    def test_on_fail_inject_contains_error(self):
        registry, mock_scheduler = self._make_registry()
        info = self._make_info("sub-002", parent="cli:root")
        registry.register(info)

        registry.on_fail("sub-002", "connection timeout")

        request = mock_scheduler.inject_turn.call_args[0][0]
        assert "connection timeout" in request.text

    def test_cancel_running(self):
        registry, _ = self._make_registry()
        info = self._make_info("sub-003")
        registry.register(info)

        mock_bg_task = MagicMock()
        mock_bg_task.done.return_value = False
        info.bg_task = mock_bg_task

        result = registry.cancel("sub-003")

        assert result is True
        assert info.status == "cancelled"
        mock_bg_task.cancel.assert_called_once()

    def test_cancel_already_completed_is_noop(self):
        registry, _ = self._make_registry()
        info = self._make_info("sub-004")
        info.status = "completed"
        registry.register(info)

        result = registry.cancel("sub-004")

        assert result is True
        assert info.status == "completed"  # 不变

    def test_cancel_nonexistent_returns_false(self):
        registry, _ = self._make_registry()
        result = registry.cancel("ghost-id")
        assert result is False

    def test_cancel_propagates_to_scheduler(self):
        """cancel() 应调用 scheduler.cancel_session_tasks(sub_session_id)。"""
        registry, mock_scheduler = self._make_registry()
        mock_scheduler.cancel_session_tasks = MagicMock()
        info = self._make_info("sub-003")
        registry.register(info)
        info.bg_task = MagicMock()
        info.bg_task.done.return_value = False

        registry.cancel("sub-003")

        mock_scheduler.cancel_session_tasks.assert_called_once_with("sub:sub-003")

    def test_on_complete_ignored_after_cancel(self):
        """on_complete 在 subagent 已 cancelled 时不注入消息。"""
        registry, mock_scheduler = self._make_registry()
        info = self._make_info("sub-001")
        registry.register(info)
        info.status = "cancelled"

        registry.on_complete("sub-001", "late result")

        mock_scheduler.inject_turn.assert_not_called()

    def test_on_fail_ignored_after_cancel(self):
        """on_fail 在 subagent 已 cancelled 时不注入消息。"""
        registry, mock_scheduler = self._make_registry()
        info = self._make_info("sub-002")
        registry.register(info)
        info.status = "cancelled"

        registry.on_fail("sub-002", "late error")

        mock_scheduler.inject_turn.assert_not_called()

    def test_on_complete_unknown_subagent_no_crash(self):
        registry, mock_scheduler = self._make_registry()
        registry.on_complete("ghost-id", "result")
        mock_scheduler.inject_turn.assert_not_called()

    def test_remove(self):
        registry, _ = self._make_registry()
        info = self._make_info("sub-005")
        registry.register(info)
        registry.remove("sub-005")
        assert registry.get("sub-005") is None


# ---------------------------------------------------------------------------
# inject_turn + OutputBus.has_waiter
# ---------------------------------------------------------------------------


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
        from system.kernel.scheduler import KernelScheduler
        from agent_core.kernel_interface import KernelRequest

        loop = asyncio.new_event_loop()
        try:
            async def _run():
                mock_kernel = MagicMock()
                mock_core_pool = MagicMock()
                scheduler = KernelScheduler(kernel=mock_kernel, core_pool=mock_core_pool)

                request = KernelRequest.create(
                    text="hello from subagent",
                    session_id="cli:root",
                    frontend_id="subagent",
                    priority=-1,
                )
                # inject_turn 不注册 waiter
                scheduler.inject_turn(request)

                assert scheduler.queue_size == 1
                assert not scheduler._out_bus.has_waiter(request.request_id)

            loop.run_until_complete(_run())
        finally:
            loop.close()

    @pytest.mark.asyncio
    async def test_cancelled_session_skipped_in_dispatch(self):
        """队列中的请求在 dispatch 时若 session 已取消，则跳过执行并 deliver_error。"""
        from system.kernel.scheduler import KernelScheduler
        from agent_core.kernel_interface import KernelRequest

        mock_kernel = MagicMock()
        mock_core_pool = MagicMock()
        scheduler = KernelScheduler(kernel=mock_kernel, core_pool=mock_core_pool)
        await scheduler.start()
        try:
            request = KernelRequest.create(
                text="hello",
                session_id="sub:skipme",
                frontend_id="subagent",
                priority=-1,
            )
            future = await scheduler.submit(request)
            scheduler.cancel_session_tasks("sub:skipme")

            with pytest.raises(asyncio.CancelledError):
                await future
        finally:
            await scheduler.stop()


# ---------------------------------------------------------------------------
# 工具单元测试
# ---------------------------------------------------------------------------


class TestCancelSubagentTool:
    @pytest.mark.asyncio
    async def test_cancel_existing_running(self):
        from agent_core.tools.subagent_tools import CancelSubagentTool
        from system.kernel.subagent_registry import SubagentInfo, SubagentRegistry

        registry = SubagentRegistry()
        info = SubagentInfo(
            subagent_id="abc123",
            parent_session_id="cli:root",
            task_description="test",
        )
        registry.register(info)

        tool = CancelSubagentTool(registry=registry)
        result = await tool.execute(subagent_id="abc123")

        assert result.success is True
        assert info.status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self):
        from agent_core.tools.subagent_tools import CancelSubagentTool
        from system.kernel.subagent_registry import SubagentRegistry

        registry = SubagentRegistry()
        tool = CancelSubagentTool(registry=registry)
        result = await tool.execute(subagent_id="ghost-id")

        assert result.success is False
        assert result.error == "SUBAGENT_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_cancel_missing_param(self):
        from agent_core.tools.subagent_tools import CancelSubagentTool
        from system.kernel.subagent_registry import SubagentRegistry

        registry = SubagentRegistry()
        tool = CancelSubagentTool(registry=registry)
        result = await tool.execute()

        assert result.success is False
        assert result.error == "MISSING_SUBAGENT_ID"


class TestSendMessageToAgentTool:
    @pytest.mark.asyncio
    async def test_send_message_success(self):
        from agent_core.tools.subagent_tools import SendMessageToAgentTool

        mock_scheduler = MagicMock()
        mock_scheduler.inject_turn = MagicMock()
        tool = SendMessageToAgentTool(scheduler=mock_scheduler)

        result = await tool.execute(
            session_id="shuiyuan:Osc7",
            content="Hello from test",
            __execution_context__={"session_id": "cli:root"},
        )

        assert result.success is True
        assert result.data["target_session"] == "shuiyuan:Osc7"
        assert "message_id" in result.data
        mock_scheduler.inject_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message_missing_session(self):
        from agent_core.tools.subagent_tools import SendMessageToAgentTool

        tool = SendMessageToAgentTool(scheduler=MagicMock())
        result = await tool.execute(content="Hello")
        assert result.success is False
        assert result.error == "MISSING_SESSION_ID"

    @pytest.mark.asyncio
    async def test_send_message_with_require_reply(self):
        from agent_core.tools.subagent_tools import SendMessageToAgentTool
        from agent_core.kernel_interface.action import AgentMessage

        captured_requests = []
        mock_scheduler = MagicMock()
        mock_scheduler.inject_turn = lambda req: captured_requests.append(req)
        tool = SendMessageToAgentTool(scheduler=mock_scheduler)

        result = await tool.execute(
            session_id="shuiyuan:Osc7",
            content="需要回复的查询",
            require_reply=True,
            __execution_context__={"session_id": "cli:root"},
        )

        assert result.success is True
        assert len(captured_requests) == 1
        req = captured_requests[0]
        agent_msg: AgentMessage = req.metadata["_agent_message"]
        assert agent_msg.message_type == "query"
        assert agent_msg.require_reply is True
        assert "需要回复" in req.text

    @pytest.mark.asyncio
    async def test_send_message_rejected_when_sender_cancelled(self):
        """_LazySchedulerSendMessageTool：已取消的 subagent 发送消息应被拒绝。"""
        from system.tools import build_tool_registry
        from agent_core.kernel_interface import CoreProfile
        from system.kernel.subagent_registry import SubagentRegistry, SubagentInfo

        registry = SubagentRegistry()
        registry.set_scheduler(MagicMock())
        info = SubagentInfo(
            subagent_id="abc123",
            parent_session_id="cli:root",
            task_description="test",
            status="cancelled",
        )
        registry.register(info)
        reg = build_tool_registry(
            profile=CoreProfile(mode="full"),
            subagent_registry=registry,
            core_pool=MagicMock(),
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


class TestReplyToMessageTool:
    @pytest.mark.asyncio
    async def test_reply_success(self):
        from agent_core.tools.subagent_tools import ReplyToMessageTool
        from agent_core.kernel_interface.action import AgentMessage

        captured = []
        mock_scheduler = MagicMock()
        mock_scheduler.inject_turn = lambda req: captured.append(req)
        tool = ReplyToMessageTool(scheduler=mock_scheduler)

        result = await tool.execute(
            correlation_id="msg-001",
            sender_session_id="cli:root",
            content="回复内容",
            __execution_context__={"session_id": "shuiyuan:Osc7"},
        )

        assert result.success is True
        assert len(captured) == 1
        req = captured[0]
        assert req.session_id == "cli:root"  # 发回给原发送方
        agent_msg: AgentMessage = req.metadata["_agent_message"]
        assert agent_msg.message_type == "reply"
        assert agent_msg.correlation_id == "msg-001"

    @pytest.mark.asyncio
    async def test_reply_missing_correlation_id(self):
        from agent_core.tools.subagent_tools import ReplyToMessageTool

        tool = ReplyToMessageTool(scheduler=MagicMock())
        result = await tool.execute(
            sender_session_id="cli:root",
            content="内容",
        )
        assert result.success is False
        assert result.error == "MISSING_CORRELATION_ID"


# ---------------------------------------------------------------------------
# first-done 语义集成测试
# ---------------------------------------------------------------------------


class TestFirstDoneSemantics:
    """
    验证多个并行 subagent 中先完成的先唤醒父 session（first-done）。
    """

    def test_multiple_subagents_inject_independently(self):
        from system.kernel.subagent_registry import SubagentInfo, SubagentRegistry

        injected = []
        mock_scheduler = MagicMock()
        mock_scheduler.inject_turn = lambda req: injected.append(req)

        registry = SubagentRegistry()
        registry.set_scheduler(mock_scheduler)

        # 注册 3 个并行子任务
        for i in range(3):
            info = SubagentInfo(
                subagent_id=f"sub-{i:03d}",
                parent_session_id="cli:root",
                task_description=f"Task {i}",
            )
            registry.register(info)

        # 模拟 sub-002 先完成（first-done）
        registry.on_complete("sub-002", "result from sub-002")
        assert len(injected) == 1
        assert "result from sub-002" in injected[0].text

        # sub-000 随后完成
        registry.on_complete("sub-000", "result from sub-000")
        assert len(injected) == 2

        # sub-001 被取消（不会再 inject）
        registry.cancel("sub-001")
        assert len(injected) == 2  # cancel 不触发 inject

    def test_failed_subagent_also_notifies_parent(self):
        from system.kernel.subagent_registry import SubagentInfo, SubagentRegistry

        injected = []
        mock_scheduler = MagicMock()
        mock_scheduler.inject_turn = lambda req: injected.append(req)

        registry = SubagentRegistry()
        registry.set_scheduler(mock_scheduler)

        info = SubagentInfo(
            subagent_id="sub-fail",
            parent_session_id="cli:root",
            task_description="failing task",
        )
        registry.register(info)
        registry.on_fail("sub-fail", "timeout error")

        assert len(injected) == 1
        assert "timeout error" in injected[0].text
        assert info.status == "failed"


# ---------------------------------------------------------------------------
# 工具注册测试（full vs sub mode）
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_no_registry_no_subagent_tools(self):
        from system.tools import build_tool_registry
        from agent_core.kernel_interface import CoreProfile

        profile = CoreProfile(mode="full")
        reg = build_tool_registry(profile=profile)
        for tool_name in [
            "create_subagent",
            "create_parallel_subagents",
            "send_message_to_agent",
            "reply_to_message",
            "get_subagent_status",
            "cancel_subagent",
        ]:
            assert not reg.has(tool_name), f"{tool_name} should NOT be registered without registry"

    def test_full_mode_with_registry_and_core_pool(self):
        from system.tools import build_tool_registry
        from agent_core.kernel_interface import CoreProfile
        from system.kernel.subagent_registry import SubagentRegistry

        registry = SubagentRegistry()
        mock_core_pool = MagicMock()
        profile = CoreProfile(mode="full")
        reg = build_tool_registry(
            profile=profile,
            subagent_registry=registry,
            core_pool=mock_core_pool,
        )
        assert reg.has("create_subagent")
        assert reg.has("create_parallel_subagents")
        assert reg.has("send_message_to_agent")
        assert reg.has("reply_to_message")
        assert reg.has("get_subagent_status")
        assert reg.has("cancel_subagent")

    def test_sub_mode_only_has_communication_tools(self):
        from system.tools import build_tool_registry
        from agent_core.kernel_interface import CoreProfile
        from system.kernel.subagent_registry import SubagentRegistry

        registry = SubagentRegistry()
        mock_core_pool = MagicMock()
        profile = CoreProfile(mode="sub")
        reg = build_tool_registry(
            profile=profile,
            subagent_registry=registry,
            core_pool=mock_core_pool,
        )
        # sub 模式只有通信工具，不能创建子 Agent
        assert reg.has("send_message_to_agent")
        assert reg.has("reply_to_message")
        assert not reg.has("create_subagent"), "sub mode must NOT have create_subagent"
        assert not reg.has("create_parallel_subagents"), "sub mode must NOT have create_parallel_subagents"
        assert not reg.has("get_subagent_status"), "sub mode must NOT have get_subagent_status"
        assert not reg.has("cancel_subagent"), "sub mode must NOT have cancel_subagent"

    def test_full_mode_without_core_pool_has_no_create_tools(self):
        """full 模式但没有 core_pool 时，create 系列工具不注册（无法执行后台任务）。"""
        from system.tools import build_tool_registry
        from agent_core.kernel_interface import CoreProfile
        from system.kernel.subagent_registry import SubagentRegistry

        registry = SubagentRegistry()
        profile = CoreProfile(mode="full")
        reg = build_tool_registry(
            profile=profile,
            subagent_registry=registry,
            core_pool=None,
        )
        assert not reg.has("create_subagent")
        assert not reg.has("create_parallel_subagents")
        # 通信工具仍然注册
        assert reg.has("send_message_to_agent")
        assert reg.has("reply_to_message")
        assert reg.has("get_subagent_status")
        assert reg.has("cancel_subagent")


# ---------------------------------------------------------------------------
# GetSubagentStatusTool 与 _merge_allowed_tools 单元测试
# ---------------------------------------------------------------------------


class TestGetSubagentStatusTool:
    @pytest.mark.asyncio
    async def test_get_status_running(self):
        from agent_core.tools.subagent_tools import GetSubagentStatusTool
        from system.kernel.subagent_registry import SubagentRegistry, SubagentInfo

        registry = SubagentRegistry()
        info = SubagentInfo(
            subagent_id="abc123",
            parent_session_id="cli:root",
            task_description="test task",
        )
        registry.register(info)

        tool = GetSubagentStatusTool(registry=registry)
        result = await tool.execute(subagent_id="abc123")
        assert result.success
        assert result.data["status"] == "running"
        assert result.data["subagent_id"] == "abc123"

    @pytest.mark.asyncio
    async def test_get_status_completed(self):
        from agent_core.tools.subagent_tools import GetSubagentStatusTool
        from system.kernel.subagent_registry import SubagentRegistry, SubagentInfo

        registry = SubagentRegistry()
        info = SubagentInfo(
            subagent_id="xyz789",
            parent_session_id="cli:root",
            task_description="done task",
            status="completed",
            result="report content",
        )
        registry._registry["xyz789"] = info

        tool = GetSubagentStatusTool(registry=registry)

        # 默认：仅返回 result_preview
        result = await tool.execute(subagent_id="xyz789")
        assert result.success
        assert result.data["status"] == "completed"
        assert "report content" in result.data.get("result_preview", "")
        assert "result" not in result.data or result.data.get("result") is None or "result_preview" in result.data

        # include_full_result=True：返回完整 result
        result_full = await tool.execute(subagent_id="xyz789", include_full_result=True)
        assert result_full.success
        assert result_full.data["result"] == "report content"
        assert "result_preview" not in result_full.data

    @pytest.mark.asyncio
    async def test_get_status_nonexistent(self):
        from agent_core.tools.subagent_tools import GetSubagentStatusTool
        from system.kernel.subagent_registry import SubagentRegistry

        tool = GetSubagentStatusTool(registry=SubagentRegistry())
        result = await tool.execute(subagent_id="nonexistent")
        assert not result.success
        assert result.error == "SUBAGENT_NOT_FOUND"


class TestMergeAllowedTools:
    def test_merge_adds_communication_tools(self):
        from agent_core.tools.subagent_tools import _merge_allowed_tools_for_subagent

        merged = _merge_allowed_tools_for_subagent(["read_file", "search_tools"])
        assert "send_message_to_agent" in merged
        assert "reply_to_message" in merged
        assert "read_file" in merged
        assert "search_tools" in merged

    def test_merge_none_returns_none(self):
        from agent_core.tools.subagent_tools import _merge_allowed_tools_for_subagent

        assert _merge_allowed_tools_for_subagent(None) is None

    def test_merge_no_duplicate_if_already_present(self):
        from agent_core.tools.subagent_tools import _merge_allowed_tools_for_subagent

        merged = _merge_allowed_tools_for_subagent(["send_message_to_agent"])
        assert merged.count("send_message_to_agent") == 1
        assert "reply_to_message" in merged


# ---------------------------------------------------------------------------
# CreateSubagentTool 单元测试
# ---------------------------------------------------------------------------


class TestCreateSubagentTool:
    @pytest.mark.asyncio
    async def test_create_subagent_missing_task(self):
        from agent_core.tools.subagent_tools import CreateSubagentTool
        from system.kernel.subagent_registry import SubagentRegistry

        registry = SubagentRegistry()
        mock_scheduler = MagicMock()
        registry.set_scheduler(mock_scheduler)

        tool = CreateSubagentTool(
            registry=registry,
            core_pool=MagicMock(),
            scheduler=mock_scheduler,
        )
        result = await tool.execute()
        assert result.success is False
        assert result.error == "MISSING_TASK"

    @pytest.mark.asyncio
    async def test_create_subagent_registers_info(self):
        from agent_core.tools.subagent_tools import CreateSubagentTool
        from system.kernel.subagent_registry import SubagentRegistry

        registry = SubagentRegistry()
        mock_scheduler = MagicMock()
        mock_scheduler.inject_turn = MagicMock()
        mock_scheduler.submit = AsyncMock(return_value=asyncio.Future())
        registry.set_scheduler(mock_scheduler)

        tool = CreateSubagentTool(
            registry=registry,
            core_pool=MagicMock(),
            scheduler=mock_scheduler,
        )
        result = await tool.execute(
            task="分析报告",
            __execution_context__={"session_id": "cli:root"},
        )

        assert result.success is True
        assert "subagent_id" in result.data
        subagent_id = result.data["subagent_id"]
        assert registry.get(subagent_id) is not None
        assert registry.get(subagent_id).parent_session_id == "cli:root"
        assert registry.get(subagent_id).status == "running"


# ---------------------------------------------------------------------------
# CreateParallelSubagentsTool 单元测试
# ---------------------------------------------------------------------------


class TestCreateParallelSubagentsTool:
    @pytest.mark.asyncio
    async def test_parallel_creates_multiple(self):
        from agent_core.tools.subagent_tools import CreateParallelSubagentsTool
        from system.kernel.subagent_registry import SubagentRegistry

        registry = SubagentRegistry()
        mock_scheduler = MagicMock()
        mock_scheduler.submit = AsyncMock(return_value=asyncio.Future())

        tool = CreateParallelSubagentsTool(
            registry=registry,
            core_pool=MagicMock(),
            scheduler=mock_scheduler,
        )
        result = await tool.execute(
            tasks=[
                {"task": "任务 A"},
                {"task": "任务 B"},
                {"task": "任务 C"},
            ],
            __execution_context__={"session_id": "cli:root"},
        )

        assert result.success is True
        assert result.data["count"] == 3
        assert len(result.data["subagent_ids"]) == 3
        # 所有子任务均已注册
        for sid in result.data["subagent_ids"]:
            assert registry.get(sid) is not None

    @pytest.mark.asyncio
    async def test_parallel_missing_tasks(self):
        from agent_core.tools.subagent_tools import CreateParallelSubagentsTool
        from system.kernel.subagent_registry import SubagentRegistry

        registry = SubagentRegistry()
        tool = CreateParallelSubagentsTool(
            registry=registry,
            core_pool=MagicMock(),
            scheduler=MagicMock(),
        )
        result = await tool.execute()
        assert result.success is False
        assert result.error == "MISSING_TASKS"
