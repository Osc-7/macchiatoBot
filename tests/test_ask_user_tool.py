"""ask_user 工具与 ask_user_registry。"""

from __future__ import annotations

import asyncio

import pytest

from agent_core.permissions.ask_user_registry import (
    AskUserAnswer,
    AskUserBatchDecision,
    cancel_ask_user_wait,
    register_ask_user_wait,
    resolve_ask_user,
    set_ask_user_notify_hook,
    submit_ask_user_fragment,
)
from agent_core.tools.ask_user_tool import AskUserTool


@pytest.mark.asyncio
async def test_ask_user_success_selected_option():
    last_bid: list[str] = []

    def _hook(bid: str, _payload: dict) -> None:
        last_bid.clear()
        last_bid.append(bid)

    set_ask_user_notify_hook(_hook)
    tool = AskUserTool()
    task = asyncio.create_task(
        tool.execute(
            questions=[
                {
                    "id": "x",
                    "prompt": "?",
                    "options": ["1", "2"],
                }
            ],
            timeout_seconds=30.0,
        )
    )
    await asyncio.sleep(0.05)
    assert len(last_bid) == 1
    ok = resolve_ask_user(
        last_bid[0],
        AskUserBatchDecision(
            answers=[AskUserAnswer(question_id="x", selected_option="1")]
        ),
    )
    assert ok
    r = await task
    assert r.success
    assert r.data and r.data.get("answers") == [{"question_id": "x", "selected_option": "1"}]
    set_ask_user_notify_hook(None)


@pytest.mark.asyncio
async def test_ask_user_custom_text():
    last_bid: list[str] = []

    def _hook(bid: str, _payload: dict) -> None:
        last_bid.clear()
        last_bid.append(bid)

    set_ask_user_notify_hook(_hook)
    tool = AskUserTool()
    task = asyncio.create_task(
        tool.execute(
            questions=[
                {
                    "id": "q1",
                    "prompt": "怎么做？",
                    "options": ["A 方案", "B 方案"],
                }
            ],
            timeout_seconds=30.0,
        )
    )
    await asyncio.sleep(0.05)
    ok = resolve_ask_user(
        last_bid[0],
        AskUserBatchDecision(
            answers=[
                AskUserAnswer(
                    question_id="q1",
                    custom_text="用我自己的方式：先备份再改",
                )
            ]
        ),
    )
    assert ok
    r = await task
    assert r.success
    assert r.data and r.data["answers"][0].get("custom_text")
    set_ask_user_notify_hook(None)


@pytest.mark.asyncio
async def test_ask_user_two_questions_default_ids():
    last_bid: list[str] = []

    def _hook(bid: str, _payload: dict) -> None:
        last_bid.clear()
        last_bid.append(bid)

    set_ask_user_notify_hook(_hook)
    tool = AskUserTool()
    task = asyncio.create_task(
        tool.execute(
            questions=[
                {
                    "id": "n1",
                    "prompt": "第一题",
                    "options": ["a", "b"],
                },
                {
                    "prompt": "第二题无 id",
                    "options": ["y", "n"],
                },
            ],
            timeout_seconds=30.0,
        )
    )
    await asyncio.sleep(0.05)
    ok = resolve_ask_user(
        last_bid[0],
        AskUserBatchDecision(
            answers=[
                AskUserAnswer(question_id="n1", selected_option="a"),
                AskUserAnswer(question_id="q2", selected_option="y"),
            ]
        ),
    )
    assert ok
    r = await task
    assert r.success
    assert len(r.data["answers"]) == 2
    set_ask_user_notify_hook(None)


@pytest.mark.asyncio
async def test_ask_user_invalid_questions():
    set_ask_user_notify_hook(lambda *_: None)
    tool = AskUserTool()
    r = await tool.execute(questions=[])
    assert not r.success
    assert r.error == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_resolve_rejects_wrong_option():
    bid, fut, _ = register_ask_user_wait(
        [{"id": "z", "prompt": "?", "options": ["ok"]}]
    )
    ok = resolve_ask_user(
        bid,
        AskUserBatchDecision(
            answers=[AskUserAnswer(question_id="z", selected_option="bad")]
        ),
    )
    assert not ok
    assert not fut.done()
    cancel_ask_user_wait(bid)


@pytest.mark.asyncio
async def test_submit_ask_user_fragment_single_question():
    """飞书点选：单题分片提交即完成。"""
    bid, fut, _ = register_ask_user_wait(
        [{"id": "only", "prompt": "?", "options": ["a", "b"]}]
    )
    ok, detail, snap = submit_ask_user_fragment(
        bid, AskUserAnswer(question_id="only", selected_option="a")
    )
    assert ok and detail == "completed"
    assert snap is not None and snap.get("done") is True
    assert fut.done()
    decision = fut.result()
    assert len(decision.answers) == 1
    assert decision.answers[0].selected_option == "a"


@pytest.mark.asyncio
async def test_ask_user_timeout():
    set_ask_user_notify_hook(lambda *_: None)
    tool = AskUserTool()
    r = await tool.execute(
        questions=[{"id": "t", "prompt": "x", "options": ["a"]}],
        timeout_seconds=0.05,
    )
    assert not r.success
    assert r.error == "ASK_USER_TIMEOUT"
