"""飞书「下一轮附件」队列。"""

from __future__ import annotations

from agent_core.content import ContentReference

from frontend.feishu.pending_attachments import (
    clear_queued_attachments,
    feishu_slash_clears_attachment_queue,
    queue_attachments_for_next_turn,
    take_queued_attachments,
)


def test_queue_take_fifo() -> None:
    sid = "feishu:test:q1"
    clear_queued_attachments(sid)
    a = ContentReference(source="feishu", ref_type="image", key="k1", extra={})
    b = ContentReference(source="feishu", ref_type="document", key="k2", extra={})
    queue_attachments_for_next_turn(sid, [a])
    queue_attachments_for_next_turn(sid, [b])
    out = take_queued_attachments(sid)
    assert [r.key for r in out] == ["k1", "k2"]
    assert take_queued_attachments(sid) == []


def test_clear_queued() -> None:
    sid = "feishu:test:q2"
    clear_queued_attachments(sid)
    queue_attachments_for_next_turn(
        sid,
        [ContentReference(source="feishu", ref_type="image", key="x", extra={})],
    )
    clear_queued_attachments(sid)
    assert take_queued_attachments(sid) == []


def test_feishu_slash_clears_attachment_queue() -> None:
    assert feishu_slash_clears_attachment_queue("/clear")
    assert feishu_slash_clears_attachment_queue("  /clear  ")
    assert not feishu_slash_clears_attachment_queue("/help")
    assert not feishu_slash_clears_attachment_queue("hello")
