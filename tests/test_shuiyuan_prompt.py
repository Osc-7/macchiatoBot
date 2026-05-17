"""水源提示词拼装：回复链（reply_to_post_number）"""

from frontend.shuiyuan_integration.prompt import (
    build_shuiyuan_prompt_from_context,
    invocation_reply_target_from_post,
)


def test_invocation_reply_target_from_post_parses_discourse_fields() -> None:
    n, u = invocation_reply_target_from_post(
        {"reply_to_post_number": 1003, "reply_to_user": {"username": "foo"}}
    )
    assert n == 1003
    assert u == "foo"


def test_invocation_reply_target_from_post_string_number() -> None:
    n, u = invocation_reply_target_from_post(
        {"reply_to_post_number": "42", "reply_to_user": {}}
    )
    assert n == 42
    assert u is None


def test_invocation_reply_target_from_post_missing() -> None:
    assert invocation_reply_target_from_post(None) == (None, None)
    assert invocation_reply_target_from_post({}) == (None, None)


def test_build_prompt_includes_reply_chain_in_footer() -> None:
    text = build_shuiyuan_prompt_from_context(
        context={
            "username": "tamaG",
            "topic_id": 471712,
            "reply_to_post_number": 1004,
            "reply_to_post_id": 8910850,
            "invocation_reply_to_post_number": 1003,
            "invocation_reply_to_username": "草上飞",
            "thread_posts": [],
        },
        user_message="【玛奇朵】你好",
    )
    assert "回复第 1003 层" in text
    assert "草上飞" in text
    assert "第 1004 层" in text


def test_build_prompt_can_derive_chain_from_invocation_post() -> None:
    text = build_shuiyuan_prompt_from_context(
        context={
            "username": "u",
            "topic_id": 1,
            "reply_to_post_number": 2,
            "reply_to_post_id": 99,
            "invocation_post": {"reply_to_post_number": 1, "reply_to_user": None},
            "thread_posts": [],
        },
        user_message="hi",
    )
    assert "回复第 1 层" in text
