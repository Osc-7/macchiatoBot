"""Tests for InternalLoader message assembly."""

from agent_core.kernel_interface.loader import _strip_orphan_tool_messages


def test_strip_orphan_keeps_parallel_tool_chain():
    msgs = [
        {"role": "assistant", "content": "x", "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": "{}"},
        {"role": "tool", "tool_call_id": "b", "content": "{}"},
    ]
    out = _strip_orphan_tool_messages(msgs)
    assert len(out) == 3
    assert [m.get("role") for m in out] == ["assistant", "tool", "tool"]


def test_strip_orphan_skips_tool_without_assistant():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "orphan", "content": "{}"},
    ]
    out = _strip_orphan_tool_messages(msgs)
    assert len(out) == 1
    assert out[0]["role"] == "user"


def test_strip_orphan_skips_tool_after_user_between_results():
    msgs = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "a"}, {"id": "b"}],
        },
        {"role": "tool", "tool_call_id": "a", "content": "{}"},
        {"role": "user", "content": "inject"},
        {"role": "tool", "tool_call_id": "b", "content": "{}"},
    ]
    out = _strip_orphan_tool_messages(msgs)
    assert [m.get("role") for m in out] == ["assistant", "tool", "user"]
