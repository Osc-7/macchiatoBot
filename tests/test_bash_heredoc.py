"""多行命令（heredoc / 多行循环）在持久化 bash 里的执行回归。

背景：agent 用 `python3 - <<'EOF' … EOF` 时，工具返回 return_code=2 且 stderr 为空，
导致它误判环境损坏，转而用 `python3 -c` 单行绕路（浪费 2–3 步）。

这里直接驱动 BashRuntime（不经 agent），锁定「多行命令能拿回正确退出码与输出」。
"""
from __future__ import annotations

import asyncio

import pytest

from agent_core.bash_runtime import BashRuntime, BashRuntimeConfig


def _runtime() -> BashRuntime:
    return BashRuntime(BashRuntimeConfig())


@pytest.mark.asyncio
async def test_heredoc_python() -> None:
    runtime = _runtime()
    await runtime.start()
    try:
        command = "python3 - <<'EOF'\nprint('heredoc-ok')\nEOF"
        result = await runtime.execute(command, timeout=20)
        assert result.exit_code == 0, f"exit={result.exit_code} stderr={result.stderr!r}"
        assert "heredoc-ok" in result.stdout
        assert result.stderr.strip() == ""
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_heredoc_cat() -> None:
    runtime = _runtime()
    await runtime.start()
    try:
        command = "cat <<'P'\nalpha\nbeta\nP"
        result = await runtime.execute(command, timeout=20)
        assert result.exit_code == 0, f"exit={result.exit_code} stderr={result.stderr!r}"
        assert "alpha" in result.stdout and "beta" in result.stdout
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_multiline_for_loop() -> None:
    runtime = _runtime()
    await runtime.start()
    try:
        command = "for i in 1 2 3\ndo\n  echo \"n=$i\"\ndone"
        result = await runtime.execute(command, timeout=20)
        assert result.exit_code == 0, f"exit={result.exit_code} stderr={result.stderr!r}"
        assert "n=1" in result.stdout and "n=3" in result.stdout
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_execute_after_heredoc_keeps_state() -> None:
    """heredoc 之后 shell 仍然可用（哨兵解析没被带偏）。"""
    runtime = _runtime()
    await runtime.start()
    try:
        first = await runtime.execute("python3 - <<'EOF'\nprint(1)\nEOF", timeout=20)
        second = await runtime.execute("echo after", timeout=20)
        assert first.exit_code == 0
        assert second.exit_code == 0
        assert "after" in second.stdout
    finally:
        await runtime.close()
