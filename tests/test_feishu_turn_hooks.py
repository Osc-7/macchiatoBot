"""飞书流式回复框 vs tool card 的出现顺序。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_core.interfaces import AgentRunResult
from frontend.feishu.feishu_turn_hooks import FeishuTurnHooksController


class _FakeFeishu:
    def __init__(self) -> None:
        self.ops: list[str] = []
        self.replace_error: Exception | None = None

    async def create_cardkit_card_entity(self, card):
        self.ops.append("create_shell")
        return "ck_1"

    async def send_message_with_card_id(self, *, chat_id: str, card_id: str) -> str:
        self.ops.append(f"send_shell:{card_id}")
        return "om_shell"

    async def cardkit_put_streaming_text_content(self, **kwargs):
        self.ops.append("put:" + str(kwargs.get("content") or "")[:40])

    async def cardkit_patch_card_settings(self, **kwargs):
        settings = kwargs.get("settings") or {}
        cfg = settings.get("config") if isinstance(settings, dict) else {}
        mode = cfg.get("streaming_mode") if isinstance(cfg, dict) else None
        self.ops.append(f"patch_settings:streaming_mode={mode}")

    async def cardkit_replace_card_entity(self, **kwargs):
        if self.replace_error is not None:
            raise self.replace_error
        card = kwargs.get("card") or {}
        tags = ((card.get("header") or {}).get("text_tag_list") or [{}])[0]
        phase = ((tags.get("text") or {}).get("content") or "")
        self.ops.append(f"replace:{phase}")

    async def send_interactive_card(self, *, chat_id: str, card) -> str:
        blob = str(card)
        kind = "tool" if "running" in blob or "Input" in blob else "reply"
        self.ops.append(f"send_card:{kind}")
        return f"om_{kind}"

    async def patch_interactive_card_message(self, **kwargs):
        self.ops.append("patch")

    async def send_reply_attachment(self, *, chat_id: str, att: dict) -> tuple[bool, str]:
        self.ops.append(f"send_attachment:{att.get('type')}:{att.get('path')}")
        return True, ""


def _ctrl(monkeypatch, fake: _FakeFeishu) -> FeishuTurnHooksController:
    fei = SimpleNamespace(
        timeout_seconds=10.0,
        reply_format="markdown_card",
        assistant_reply_stream=True,
        assistant_cardkit_stream=True,
        tool_trace_cards_enabled=True,
    )
    monkeypatch.setattr(
        "frontend.feishu.feishu_turn_hooks.get_config",
        lambda: SimpleNamespace(feishu=fei),
    )
    return FeishuTurnHooksController(chat_id="oc_test", feishu_client=fake)


@pytest.mark.asyncio
async def test_whitespace_delta_does_not_open_stream_shell_before_tool(monkeypatch):
    """思考模型先吐换行时，不应先冒空白回复框再在下面跑 tool。"""
    fake = _FakeFeishu()
    ctrl = _ctrl(monkeypatch, fake)

    await ctrl._on_assistant_delta("\n")
    await ctrl._on_assistant_delta("  ")
    await ctrl._on_trace_event(
        {
            "type": "tool_call",
            "name": "bash",
            "tool_call_id": "call_1",
            "arguments": {"command": "date"},
        }
    )

    assert "create_shell" not in fake.ops
    assert fake.ops == ["send_card:tool"]
    assert ctrl._ck_card_id == ""


@pytest.mark.asyncio
async def test_visible_text_then_tool_closes_segment_before_tool_card(monkeypatch):
    fake = _FakeFeishu()
    ctrl = _ctrl(monkeypatch, fake)

    await ctrl._on_assistant_delta("先说一句再调工具")
    await ctrl._on_trace_event(
        {
            "type": "tool_call",
            "name": "bash",
            "tool_call_id": "call_2",
            "arguments": {"command": "echo hi"},
        }
    )

    assert fake.ops[0] == "create_shell"
    assert fake.ops[-1] == "send_card:tool"
    assert "patch_settings:streaming_mode=False" in fake.ops
    assert "replace:Segment" in fake.ops
    assert fake.ops.index("patch_settings:streaming_mode=False") < fake.ops.index(
        "replace:Segment"
    )
    assert fake.ops.index("replace:Segment") < fake.ops.index("send_card:tool")
    assert ctrl._ck_card_id == ""
    assert ctrl._assistant_buffer == ""


@pytest.mark.asyncio
async def test_finalize_stops_streaming_mode_then_replaces_complete(monkeypatch):
    """关流必须先 PATCH settings，否则会话标题会一直停在 (Streaming)。"""
    fake = _FakeFeishu()
    ctrl = _ctrl(monkeypatch, fake)

    await ctrl._on_assistant_delta("搜完了，结论：cutoff 约 2025 年 5 月。")
    result = await ctrl.finalize_after_run(
        AgentRunResult(output_text="搜完了，结论：cutoff 约 2025 年 5 月。")
    )

    assert "patch_settings:streaming_mode=False" in fake.ops
    assert "replace:Complete" in fake.ops
    assert fake.ops.index("patch_settings:streaming_mode=False") < fake.ops.index(
        "replace:Complete"
    )
    assert result.metadata.get("feishu_skip_final_reply") is True
    assert ctrl._ck_card_id == ""


@pytest.mark.asyncio
async def test_finalize_falls_back_to_message_patch_if_replace_fails(monkeypatch):
    fake = _FakeFeishu()
    fake.replace_error = RuntimeError("replace rejected while streaming")
    ctrl = _ctrl(monkeypatch, fake)

    await ctrl._on_assistant_delta("搜完了，结论：cutoff 约 2025 年 5 月。")
    result = await ctrl.finalize_after_run(
        AgentRunResult(output_text="搜完了，结论：cutoff 约 2025 年 5 月。")
    )

    assert "patch_settings:streaming_mode=False" in fake.ops
    assert "patch" in fake.ops
    assert result.metadata.get("feishu_skip_final_reply") is True
    assert ctrl._ck_card_id == ""


@pytest.mark.asyncio
async def test_outgoing_attachment_is_sent_immediately(monkeypatch):
    fake = _FakeFeishu()
    ctrl = _ctrl(monkeypatch, fake)

    ok = await ctrl._on_outgoing_attachment(
        {"type": "file", "path": "/tmp/report.pdf", "file_name": "report.pdf"}
    )

    assert ok is True
    assert fake.ops == ["send_attachment:file:/tmp/report.pdf"]
