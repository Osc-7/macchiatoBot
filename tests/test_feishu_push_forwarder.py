"""Feishu push/recovery forwarding tests."""

from __future__ import annotations

import pytest

from frontend.feishu import ipc_bridge


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
