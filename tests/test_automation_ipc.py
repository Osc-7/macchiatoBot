"""Automation IPC tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.interfaces import AgentHooks, AgentRunInput, AgentRunResult
from system.automation import (
    AutomationCoreGateway,
    AutomationIPCClient,
    AutomationIPCServer,
    SessionRegistry,
)
from system.kernel import CorePool


def _make_ipc_mock_scheduler(work_result: AgentRunResult, work_usage: dict):
    """创建支持 hooks 调用的 mock scheduler，用于 IPC 测试。"""
    last_request = []

    async def _submit(request):
        last_request.append(request)
        sid = getattr(request, "session_id", "cli:root")

        class Handle:
            request_id = getattr(request, "request_id", "mock-req-id")
            session_id = sid

            def __await__(self):
                return _wait_result(self).__await__()

        return Handle()

    async def _wait_result(handle):
        req = last_request[-1] if last_request else None
        metadata = getattr(req, "metadata", {}) or {}
        hooks = metadata.get("_hooks")
        if hooks:
            if hooks.on_trace_event:
                await hooks.on_trace_event(
                    {"type": "llm_request", "iteration": 1, "tool_count": 3}
                )
            if hooks.on_reasoning_delta:
                await hooks.on_reasoning_delta("thinking...")
            if hooks.on_assistant_delta:
                await hooks.on_assistant_delta("hello ")
                await hooks.on_assistant_delta("world")
        return work_result

    work_agent = MagicMock()
    work_agent.get_token_usage = MagicMock(return_value=work_usage)
    work_agent.get_turn_count = MagicMock(return_value=1)
    work_agent.clear_context = MagicMock()
    work_entry = MagicMock()
    work_entry.agent = work_agent

    mock_pool = MagicMock(spec=CorePool)
    mock_pool.list_sessions = MagicMock(return_value=["cli:root", "cli:work"])
    # has_session 返回 False，使 switch_session 时 created=True（session 由 gateway 创建）
    mock_pool.has_session = MagicMock(return_value=False)
    mock_pool.evict = AsyncMock()
    mock_pool.get_entry = MagicMock(
        side_effect=lambda sid: work_entry if sid == "cli:work" else None
    )

    mock_scheduler = MagicMock()
    mock_scheduler.submit = AsyncMock(side_effect=_submit)
    mock_scheduler.wait_result = AsyncMock(side_effect=_wait_result)
    mock_scheduler.core_pool = mock_pool
    mock_scheduler.subscribe_out = MagicMock(return_value="mock-sub-id")
    mock_scheduler.unsubscribe_out = MagicMock()

    return mock_scheduler, work_agent


@pytest.mark.asyncio
async def test_run_turn_stream_client_disconnect_does_not_abort_gateway(
    tmp_path: Path,
):
    class DisconnectingWriter:
        def write(self, _data: bytes) -> None:
            pass

        async def drain(self) -> None:
            raise ConnectionResetError("Connection lost")

    class Gateway:
        completed = False

        async def inject_message(self, command, hooks=None):  # type: ignore[no-untyped-def]
            assert command.session_id == "cli:root"
            if hooks and hooks.on_trace_event:
                await hooks.on_trace_event({"type": "tool_result", "name": "bash"})
            self.completed = True
            return AgentRunResult(output_text="done")

        def get_token_usage(self, *, session_id: str) -> dict:
            assert session_id == "cli:root"
            return {}

        def get_turn_count(self, *, session_id: str) -> int:
            assert session_id == "cli:root"
            return 1

    gateway = Gateway()
    server = AutomationIPCServer(
        gateway,  # type: ignore[arg-type]
        owner_id="root",
        source="cli",
        socket_path=str(tmp_path / "automation.sock"),
    )

    await server._handle_run_turn_stream(
        "req-1",
        {"client_id": "client-a", "text": "hello", "metadata": {}},
        DisconnectingWriter(),  # type: ignore[arg-type]
    )

    assert gateway.completed is True


@pytest.mark.asyncio
async def test_run_turn_stream_client_disconnect_buffers_recoverable_result(
    tmp_path: Path,
):
    class DisconnectingWriter:
        def write(self, _data: bytes) -> None:
            pass

        async def drain(self) -> None:
            raise ConnectionResetError("Connection lost")

    class Gateway:
        async def inject_message(self, command, hooks=None):  # type: ignore[no-untyped-def]
            if hooks and hooks.on_trace_event:
                await hooks.on_trace_event({"type": "tool_result", "name": "bash"})
            return AgentRunResult(
                output_text="done",
                metadata={"custom": "value"},
                attachments=[{"type": "file", "path": "/tmp/a.pdf"}],
            )

        def get_token_usage(self, *, session_id: str) -> dict:
            return {}

        def get_turn_count(self, *, session_id: str) -> int:
            return 1

    server = AutomationIPCServer(
        Gateway(),  # type: ignore[arg-type]
        owner_id="root",
        source="cli",
        socket_path=str(tmp_path / "automation.sock"),
    )

    await server._handle_run_turn_stream(
        "req-1",
        {
            "client_id": "client-a",
            "text": "hello",
            "metadata": {"feishu_chat_id": "oc_test"},
        },
        DisconnectingWriter(),  # type: ignore[arg-type]
    )

    data = await server._dispatch("poll_stream_recoveries", {})
    assert data["results"] == [
        {
            "request_id": "req-1",
            "session_id": "cli:root",
            "output_text": "done",
            "metadata": {
                "feishu_chat_id": "oc_test",
                "custom": "value",
                "_stream_recovery": True,
            },
            "attachments": [{"type": "file", "path": "/tmp/a.pdf"}],
        }
    ]
    assert await server._dispatch("poll_stream_recoveries", {}) == {"results": []}


@pytest.mark.asyncio
async def test_run_turn_stream_error_keeps_non_empty_message(tmp_path: Path):
    class SilentReadTimeout(Exception):
        def __str__(self) -> str:
            return ""

    class Gateway:
        async def inject_message(self, command, hooks=None):  # type: ignore[no-untyped-def]
            _ = command
            _ = hooks
            raise SilentReadTimeout()

        def get_token_usage(self, *, session_id: str) -> dict:
            _ = session_id
            return {}

        def get_turn_count(self, *, session_id: str) -> int:
            _ = session_id
            return 0

    socket_path = str(tmp_path / "automation.sock")
    server = AutomationIPCServer(
        Gateway(),  # type: ignore[arg-type]
        owner_id="root",
        source="cli",
        socket_path=socket_path,
    )
    await server.start()
    client = AutomationIPCClient(owner_id="root", source="cli", socket_path=socket_path)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            await client.run_turn(AgentRunInput(text="hello"))
        assert "automation ipc error" not in str(exc_info.value)
        assert "SilentReadTimeout" in str(exc_info.value)
    finally:
        await client.close()
        await server.stop()


@pytest.mark.asyncio
async def test_ipc_server_client_run_turn_and_session_commands(tmp_path: Path):
    default_core = AsyncMock()
    default_core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    default_core.close = AsyncMock()

    work_result = AgentRunResult(output_text="work-ok")
    work_usage = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "call_count": 1,
        "cost_yuan": 0.0,
    }
    scheduler, work_agent = _make_ipc_mock_scheduler(work_result, work_usage)

    gateway = AutomationCoreGateway(
        default_core,
        kernel_scheduler=scheduler,
        session_id="cli:root",
        session_registry=SessionRegistry(str(tmp_path / "sessions.db")),
    )

    socket_path = str(tmp_path / "automation.sock")
    server = AutomationIPCServer(
        gateway, owner_id="root", source="cli", socket_path=socket_path
    )
    await server.start()
    client = AutomationIPCClient(owner_id="root", source="cli", socket_path=socket_path)
    try:
        assert await client.ping() is True
        await client.connect()

        sessions = await client.list_sessions()
        assert "cli:root" in sessions

        created = await client.switch_session("cli:work", create_if_missing=True)
        assert created is True
        assert client.active_session_id == "cli:work"

        trace_events: list[dict] = []
        assistant_deltas: list[str] = []
        reasoning_deltas: list[str] = []

        async def _on_trace_event(evt: dict) -> None:
            trace_events.append(evt)

        async def _on_assistant_delta(delta: str) -> None:
            assistant_deltas.append(delta)

        async def _on_reasoning_delta(delta: str) -> None:
            reasoning_deltas.append(delta)

        result = await client.run_turn(
            AgentRunInput(text="hello"),
            hooks=AgentHooks(
                on_trace_event=_on_trace_event,
                on_assistant_delta=_on_assistant_delta,
                on_reasoning_delta=_on_reasoning_delta,
            ),
        )
        assert result.output_text == "work-ok"
        assert isinstance(trace_events, list)
        assert "".join(assistant_deltas) == "hello world"
        assert reasoning_deltas == ["thinking..."]

        usage = await client.get_token_usage()
        assert usage["total_tokens"] == 15

        await client.clear_context()
        work_agent.clear_context.assert_called_once()
    finally:
        await client.close()
        await server.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_ipc_client_expire_session_calls_gateway_evict(tmp_path: Path):
    default_core = AsyncMock()
    default_core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    default_core.close = AsyncMock()

    scheduler, _work_agent = _make_ipc_mock_scheduler(
        AgentRunResult(output_text="work-ok"),
        {"total_tokens": 0, "call_count": 0},
    )
    gateway = AutomationCoreGateway(
        default_core,
        kernel_scheduler=scheduler,
        session_id="cli:root",
        session_registry=SessionRegistry(str(tmp_path / "sessions.db")),
    )
    socket_path = str(tmp_path / "automation.sock")
    server = AutomationIPCServer(
        gateway, owner_id="root", source="cli", socket_path=socket_path
    )
    await server.start()
    client = AutomationIPCClient(owner_id="root", source="cli", socket_path=socket_path)
    try:
        await client.connect()
        await client.switch_session("cli:work", create_if_missing=True)

        expired = await client.expire_session("cli:work", reason="manual_new")

        assert expired is True
        scheduler.core_pool.evict.assert_awaited_once_with("cli:work")
    finally:
        await client.close()
        await server.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_ipc_session_delete_rejected_when_session_is_active_for_any_client(
    tmp_path: Path,
):
    from tests.test_automation_core_gateway import _make_mock_scheduler

    default_core = AsyncMock()
    default_core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    default_core.close = AsyncMock()

    work_core = AsyncMock()
    work_core.get_session_state = MagicMock(return_value=MagicMock(turn_count=0))
    work_core.activate_session = AsyncMock(return_value=None)
    work_core.delete_session_history = MagicMock(return_value=1)
    work_core.close = AsyncMock()
    factory = AsyncMock(return_value=work_core)

    scheduler = _make_mock_scheduler()
    gateway = AutomationCoreGateway(
        default_core,
        kernel_scheduler=scheduler,
        session_id="cli:root",
        session_factory=factory,
        session_registry=SessionRegistry(str(tmp_path / "sessions.db")),
    )
    socket_path = str(tmp_path / "automation.sock")
    server = AutomationIPCServer(
        gateway, owner_id="root", source="cli", socket_path=socket_path
    )
    await server.start()
    client_a = AutomationIPCClient(
        owner_id="root", source="cli", socket_path=socket_path
    )
    client_b = AutomationIPCClient(
        owner_id="root", source="cli", socket_path=socket_path
    )
    try:
        await client_a.connect()
        await client_b.connect()
        await client_a.switch_session("cli:work", create_if_missing=True)
        await client_b.switch_session("cli:work", create_if_missing=True)

        deleted = await client_a.delete_session("cli:work")

        assert deleted is False
        work_core.delete_session_history.assert_not_called()
    finally:
        await client_a.close()
        await client_b.close()
        await server.stop()
        await gateway.close()


@pytest.mark.asyncio
async def test_ipc_dispatch_resolve_remote_login_feishu(monkeypatch, tmp_path: Path):
    """网关进程须通过 IPC 命中 daemon 内的 pending；dispatch 应转发到 resolve_remote_login。"""
    from system.automation import remote_worker_server as rws

    captured: dict[str, object] = {}

    def fake_resolve(**kwargs):
        captured.update(kwargs)
        return "success", "已批准该远程登录请求", {"schema": "2.0", "stub": True}

    monkeypatch.setattr(rws, "resolve_remote_login_request_from_feishu", fake_resolve)

    gateway = MagicMock()
    server = AutomationIPCServer(
        gateway,  # type: ignore[arg-type]
        owner_id="root",
        source="cli",
        socket_path=str(tmp_path / "unused.sock"),
    )

    result = await server._dispatch(
        "resolve_remote_login_feishu",
        {
            "client_id": "feishu:test",
            "request_id": "dc-001",
            "approve": True,
            "approver_open_id": "ou_x",
            "approver_user_id": "",
        },
    )

    assert result["kind"] == "success"
    assert result["message"] == "已批准该远程登录请求"
    assert result["card"] == {"schema": "2.0", "stub": True}
    assert captured["request_id"] == "dc-001"
    assert captured["approve"] is True
    assert captured["approver_open_id"] == "ou_x"
