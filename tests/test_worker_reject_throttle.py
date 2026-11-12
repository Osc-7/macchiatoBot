"""remote worker 拒绝日志的节流回归。

背景：旧机器用失效 token 每几秒重试一次，3 小时刷了 2508 条 WARNING，
把真正有用的日志淹了。现在同一 (login, reason) 10 分钟只打一条，
并把期间被压制的次数带出来。
"""
from __future__ import annotations

import logging

import pytest

from system.automation import remote_worker_server as server


@pytest.fixture()
def clock(monkeypatch):
    state = {"now": 1000.0}
    monkeypatch.setattr(server, "_rejection_clock", lambda: state["now"])
    server._rejection_log_state.clear()
    yield state
    server._rejection_log_state.clear()


def _records(caplog):
    return [r for r in caplog.records if "remote worker rejected" in r.getMessage()]


def test_same_window_logs_once(caplog, clock) -> None:
    caplog.set_level(logging.WARNING, logger=server.logger.name)

    server._log_rejection_once("sii2", "unknown_login")
    for _ in range(5):
        clock["now"] += 10
        server._log_rejection_once("sii2", "unknown_login")

    assert len(_records(caplog)) == 1, f"窗口内只该打一条，实际 {len(_records(caplog))}"


def test_next_window_reports_suppressed_count(caplog, clock) -> None:
    caplog.set_level(logging.WARNING, logger=server.logger.name)

    server._log_rejection_once("sii2", "unknown_login")
    for _ in range(5):
        clock["now"] += 10
        server._log_rejection_once("sii2", "unknown_login")

    clock["now"] += 700  # 越过 10 分钟窗口
    server._log_rejection_once("sii2", "unknown_login")

    records = _records(caplog)
    assert len(records) == 2, f"窗口过后应再打一条，实际 {len(records)}"
    assert "又拒绝 5 次" in records[-1].getMessage(), records[-1].getMessage()


def test_different_login_logged_separately(caplog, clock) -> None:
    caplog.set_level(logging.WARNING, logger=server.logger.name)

    server._log_rejection_once("sii2", "unknown_login")
    server._log_rejection_once("g1", "unknown_login")

    assert len(_records(caplog)) == 2
