"""Feishu push/recovery forwarding tests."""

from __future__ import annotations

import pytest

from agent_core.interfaces import AgentRunResult
from frontend.feishu import ipc_bridge
from system.automation import core_gateway
from system.automation.core_gateway import (
    _PUSH_SEND_ATTEMPT_LIMIT,
    AutomationCoreGateway,
)


@pytest.fixture(autouse=True)
def _isolate_feishu_chat_store(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ipc_bridge,
        "_default_feishu_chat_store_path",
        lambda: tmp_path / "session_chats.json",
    )


@pytest.mark.asyncio
async def test_push_forwarder_polls_stream_recoveries_without_registered_sessions(
    monkeypatch: pytest.MonkeyPatch,
):
    sent: list[tuple[str, str]] = []

    class FakeIPCClient:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        async def ping(self) -> bool:
            return True

        async def poll_stream_recoveries(self) -> list:
            return [
                {
                    "session_id": "feishu:user:ou_1",
                    "output_text": "done",
                    "metadata": {"feishu_chat_id": "oc_1"},
                    "attachments": [],
                }
            ]

        async def switch_session(
            self, session_id: str, *, create_if_missing: bool = False
        ) -> bool:
            raise AssertionError("registered session polling should not be required")

    class FakeFeishuClient:
        def __init__(self, *, timeout_seconds: float = 10.0) -> None:
            self.timeout_seconds = timeout_seconds

    async def fake_send_final_reply(*, client, chat_id: str, output_text: str):  # type: ignore[no-untyped-def]
        sent.append((chat_id, output_text))

    monkeypatch.setattr(ipc_bridge, "AutomationIPCClient", FakeIPCClient)
    monkeypatch.setattr(ipc_bridge, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(
        ipc_bridge, "send_feishu_agent_final_reply", fake_send_final_reply
    )

    forwarder = ipc_bridge.FeishuPushForwarder(poll_interval_seconds=999)
    await forwarder._poll_once()

    assert sent == [("oc_1", "done")]


@pytest.mark.asyncio
async def test_push_forwarder_still_sends_attachments_when_final_reply_skipped(
    monkeypatch: pytest.MonkeyPatch,
):
    sent_text: list[str] = []
    sent_attachments: list[list] = []

    class FakeIPCClient:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        async def ping(self) -> bool:
            return True

        async def poll_stream_recoveries(self) -> list:
            return [
                {
                    "session_id": "feishu:user:ou_1",
                    "output_text": "done",
                    "metadata": {
                        "feishu_chat_id": "oc_1",
                        "feishu_skip_final_reply": True,
                    },
                    "attachments": [{"type": "file", "path": "/tmp/a.pdf"}],
                }
            ]

        async def switch_session(
            self, session_id: str, *, create_if_missing: bool = False
        ) -> bool:
            raise AssertionError("registered session polling should not be required")

    class FakeFeishuClient:
        def __init__(self, *, timeout_seconds: float = 10.0) -> None:
            self.timeout_seconds = timeout_seconds

        async def send_reply_attachments(self, *, chat_id: str, attachments: list):
            sent_attachments.append(list(attachments))

    async def fake_send_final_reply(*, client, chat_id: str, output_text: str):  # type: ignore[no-untyped-def]
        sent_text.append(output_text)

    monkeypatch.setattr(ipc_bridge, "AutomationIPCClient", FakeIPCClient)
    monkeypatch.setattr(ipc_bridge, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(
        ipc_bridge, "send_feishu_agent_final_reply", fake_send_final_reply
    )

    forwarder = ipc_bridge.FeishuPushForwarder(poll_interval_seconds=999)
    await forwarder._poll_once()

    assert sent_text == []
    assert sent_attachments == [[{"type": "file", "path": "/tmp/a.pdf"}]]


def _bare_gateway() -> AutomationCoreGateway:
    gateway = object.__new__(AutomationCoreGateway)
    gateway._pending_submits = set()
    gateway._push_buffers = {}
    return gateway


def test_group_session_chat_id_does_not_need_a_prior_registration() -> None:
    forwarder = ipc_bridge.FeishuPushForwarder(poll_interval_seconds=999)
    assert forwarder.chat_id_for_session("feishu:chat:oc_group") == "oc_group"
    assert forwarder.chat_id_for_session("feishu:user:ou_1") == ""
    forwarder.remember_chat("feishu:user:ou_1", "oc_p2p")
    assert forwarder.chat_id_for_session("feishu:user:ou_1") == "oc_p2p"


def test_pending_push_sessions_are_frontend_agnostic() -> None:
    gateway = _bare_gateway()
    gateway._on_session_output(
        "cli:root",
        "req-1",
        AgentRunResult(output_text="done", metadata={}),
    )
    assert gateway.pending_push_sessions() == ["cli:root"]
    request_id, _result = gateway.poll_push_result("cli:root")
    assert request_id == "req-1"
    assert gateway.pending_push_sessions() == []


def test_lease_frontend_push_keeps_items_until_ack() -> None:
    gateway = _bare_gateway()
    gateway._on_session_output(
        "feishu:user:ou_1",
        "req-feishu",
        AgentRunResult(output_text="feishu", metadata={}),
    )
    gateway._on_session_output(
        "shuiyuan:alice",
        "req-sy",
        AgentRunResult(output_text="shuiyuan", metadata={}),
    )
    leased = gateway.lease_frontend_push("feishu")
    assert [item[0] for item in leased] == ["feishu:user:ou_1"]
    assert gateway.lease_frontend_push("feishu") == []
    assert set(gateway.pending_push_sessions()) == {
        "feishu:user:ou_1",
        "shuiyuan:alice",
    }
    assert gateway.poll_push_result("feishu:user:ou_1") is None
    assert gateway.ack_push_payloads("feishu:user:ou_1", ["req-feishu"]) == 1
    assert gateway.pending_push_sessions() == ["shuiyuan:alice"]


def test_expired_lease_is_offered_again(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(core_gateway.time, "monotonic", lambda: clock["now"])
    gateway = _bare_gateway()
    gateway._on_session_output(
        "feishu:user:ou_1",
        "req-feishu",
        AgentRunResult(output_text="still here", metadata={}),
    )
    assert len(gateway.lease_frontend_push("feishu", lease_seconds=10)) == 1
    clock["now"] = 1011.0
    again = gateway.lease_frontend_push("feishu", lease_seconds=10)
    assert [item[1] for item in again] == ["req-feishu"]
    assert gateway.pending_push_sessions() == ["feishu:user:ou_1"]


def test_release_without_attempt_does_not_drop_the_result() -> None:
    gateway = _bare_gateway()
    gateway._on_session_output(
        "feishu:user:ou_1",
        "req-feishu",
        AgentRunResult(output_text="waiting for chat", metadata={}),
    )
    gateway.lease_frontend_push("feishu")
    assert (
        gateway.release_push_leases(
            "feishu:user:ou_1", ["req-feishu"], count_attempt=False
        )
        == 1
    )
    item = gateway._push_buffers["feishu:user:ou_1"][0]
    assert item.attempts == 0
    assert item.leased_until == 0
    assert len(gateway.lease_frontend_push("feishu")) == 1
    assert gateway._push_buffers["feishu:user:ou_1"][0].attempts == 0


def test_release_drops_after_attempt_limit() -> None:
    gateway = _bare_gateway()
    gateway._on_session_output(
        "cli:root",
        "req-1",
        AgentRunResult(output_text="done", metadata={}),
    )
    gateway.lease_frontend_push("cli")
    gateway._push_buffers["cli:root"][0].attempts = _PUSH_SEND_ATTEMPT_LIMIT
    assert gateway.release_push_leases("cli:root", ["req-1"]) == 0
    assert gateway.pending_push_sessions() == []


@pytest.mark.asyncio
async def test_push_forwarder_drains_pending_without_registration(
    monkeypatch: pytest.MonkeyPatch,
):
    sent: list[str] = []
    polled: list[str] = []
    acked: list[tuple[str, list[str]]] = []

    class FakeIPCClient:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        async def ping(self) -> bool:
            return True

        async def poll_stream_recoveries(self) -> list:
            return []

        async def poll_frontend_push(self) -> list:
            polled.append("feishu")
            return [
                {
                    "request_id": "req-old",
                    "session_id": "feishu:user:ou_old",
                    "output_text": "",
                    "metadata": {
                        "_error_info": {
                            "kind": "rate_limit",
                            "label": "上游限流",
                            "hint": "短时并发/限流，通常几十秒到几分钟恢复。",
                        }
                    },
                    "attachments": [],
                }
            ]

        async def ack_push(self, session_id: str, request_ids: list) -> int:
            acked.append((session_id, list(request_ids)))
            return len(request_ids)

        async def switch_session(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("frontend dispatch must not switch sessions")

    class FakeFeishuClient:
        def __init__(self, *, timeout_seconds: float = 10.0) -> None:
            self.timeout_seconds = timeout_seconds

        async def send_interactive_card(self, *, chat_id: str, card: dict) -> None:
            sent.append(chat_id)

    monkeypatch.setattr(ipc_bridge, "AutomationIPCClient", FakeIPCClient)
    monkeypatch.setattr(ipc_bridge, "FeishuClient", FakeFeishuClient)

    forwarder = ipc_bridge.FeishuPushForwarder(poll_interval_seconds=999)
    forwarder.remember_chat("feishu:user:ou_old", "oc_old")
    await forwarder._poll_once()

    assert polled == ["feishu"]
    assert sent == ["oc_old"]
    assert acked == [("feishu:user:ou_old", ["req-old"])]


@pytest.mark.asyncio
async def test_push_forwarder_releases_failed_pending_send(
    monkeypatch: pytest.MonkeyPatch,
):
    released: list[tuple[str, list[str], bool]] = []

    class FakeIPCClient:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        async def ping(self) -> bool:
            return True

        async def poll_stream_recoveries(self) -> list:
            return []

        async def poll_frontend_push(self) -> list:
            return [
                {
                    "request_id": "req-1",
                    "session_id": "feishu:user:ou_old",
                    "output_text": "还没发出去",
                    "metadata": {},
                    "attachments": [],
                }
            ]

        async def release_push(
            self,
            session_id: str,
            request_ids: list,
            *,
            count_attempt: bool = True,
        ) -> int:
            released.append((session_id, list(request_ids), count_attempt))
            return len(request_ids)

    class FakeFeishuClient:
        def __init__(self, *, timeout_seconds: float = 10.0) -> None:
            self.timeout_seconds = timeout_seconds

    async def fail_send(*, client, chat_id: str, output_text: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("feishu down")

    monkeypatch.setattr(ipc_bridge, "AutomationIPCClient", FakeIPCClient)
    monkeypatch.setattr(ipc_bridge, "FeishuClient", FakeFeishuClient)
    monkeypatch.setattr(ipc_bridge, "send_feishu_agent_final_reply", fail_send)

    forwarder = ipc_bridge.FeishuPushForwarder(poll_interval_seconds=999)
    forwarder.remember_chat("feishu:user:ou_old", "oc_old")
    await forwarder._poll_once()

    assert released == [("feishu:user:ou_old", ["req-1"], True)]


def test_p2p_chat_id_survives_a_new_forwarder(tmp_path) -> None:
    store = tmp_path / "nested" / "session_chats.json"
    first = ipc_bridge.FeishuPushForwarder(
        poll_interval_seconds=999, chat_store_path=str(store)
    )
    first.remember_chat("feishu:user:ou_1", "oc_p2p")
    second = ipc_bridge.FeishuPushForwarder(
        poll_interval_seconds=999, chat_store_path=str(store)
    )
    assert second.chat_id_for_session("feishu:user:ou_1") == "oc_p2p"


@pytest.mark.asyncio
async def test_push_forwarder_releases_unrouted_without_counting(
    monkeypatch: pytest.MonkeyPatch,
):
    released: list[tuple[str, list[str], bool]] = []

    class FakeIPCClient:
        def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        async def ping(self) -> bool:
            return True

        async def poll_stream_recoveries(self) -> list:
            return []

        async def poll_frontend_push(self) -> list:
            return [
                {
                    "request_id": "req-1",
                    "session_id": "feishu:user:ou_unknown",
                    "output_text": "还不知道发给谁",
                    "metadata": {},
                    "attachments": [],
                }
            ]

        async def release_push(
            self,
            session_id: str,
            request_ids: list,
            *,
            count_attempt: bool = True,
        ) -> int:
            released.append((session_id, list(request_ids), count_attempt))
            return len(request_ids)

        async def ack_push(self, session_id: str, request_ids: list) -> int:
            raise AssertionError("unrouted push must stay queued")

    monkeypatch.setattr(ipc_bridge, "AutomationIPCClient", FakeIPCClient)
    forwarder = ipc_bridge.FeishuPushForwarder(poll_interval_seconds=999)
    await forwarder._poll_once()
    assert released == [("feishu:user:ou_unknown", ["req-1"], False)]
