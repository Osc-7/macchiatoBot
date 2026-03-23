from unittest.mock import patch

from frontend.shuiyuan_integration.reply import (
    AUTO_REPLY_MARK,
    _attach_hidden_marker,  # type: ignore[attr-defined]
    post_reply,
)
from frontend.shuiyuan_integration.session import (
    is_invocation_valid,
    is_invocation_valid_from_raw,
)


class _DummyDB:
    def __init__(self) -> None:
        self.allowed_checked = 0
        self.recorded = 0
        self.chats: list[tuple[str, int, str, str, int | None]] = []

    def check_reply_allowed(self, username: str) -> bool:
        self.allowed_checked += 1
        return True

    def record_reply(self, username: str) -> None:
        self.recorded += 1

    def append_chat(
        self,
        username: str,
        topic_id: int,
        role: str,
        content: str,
        post_id: int | None = None,
    ) -> None:
        self.chats.append((username, topic_id, role, content, post_id))


class _DummyClient:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def create_post(self, *, raw: str, topic_id: int, reply_to_post_number=None):
        self.payloads.append(
            {
                "raw": raw,
                "topic_id": topic_id,
                "reply_to_post_number": reply_to_post_number,
            }
        )
        return {"id": 123}, 200, ""


class _FlakyClient:
    """前两次 503，第三次成功（验证 post_reply 对瞬时 HTTP 的重试）。"""

    def __init__(self) -> None:
        self.calls = 0

    def create_post(self, *, raw: str, topic_id: int, reply_to_post_number=None):
        self.calls += 1
        if self.calls < 3:
            return None, 503, "service unavailable"
        return {"id": 999}, 200, ""


def test_attach_hidden_marker_appends_comment_and_mark():
    text = "你好，世界"
    out = _attach_hidden_marker(text)
    assert text in out
    assert AUTO_REPLY_MARK in out
    # 再次调用不应重复附加
    out2 = _attach_hidden_marker(out)
    assert out2 == out


def test_post_reply_attaches_marker_and_uses_it_in_db():
    db = _DummyDB()
    client = _DummyClient()

    ok, msg = post_reply(
        username="user",
        topic_id=1,
        raw="回复内容",
        db=db,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
    )

    assert ok is True
    assert "post_id=" in msg
    assert len(client.payloads) == 1
    sent_raw = client.payloads[0]["raw"]
    assert AUTO_REPLY_MARK in sent_raw

    assert len(db.chats) == 1
    _, _, role, content, _ = db.chats[0]
    assert role == "assistant"
    assert AUTO_REPLY_MARK in content


@patch("frontend.shuiyuan_integration.reply.time.sleep", lambda _: None)
def test_post_reply_retries_transient_http():
    db = _DummyDB()
    client = _FlakyClient()

    ok, msg = post_reply(
        username="user",
        topic_id=1,
        raw="回复内容",
        db=db,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
    )

    assert ok is True
    assert "999" in msg
    assert client.calls == 3


def test_is_invocation_invalid_when_marker_present():
    text = f"【玛奇朵】 {AUTO_REPLY_MARK}"

    ok1, _ = is_invocation_valid(text, mentioned_usernames=["owner"])
    assert ok1 is False

    ok2, _ = is_invocation_valid_from_raw(text)
    assert ok2 is False
