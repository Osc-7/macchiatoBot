"""
BashRuntime 单元测试。

覆盖：
- 进程生命周期（start / close / restart）
- 命令执行 + sentinel 解析
- 多命令会话持久化（cd / export 保持）
- 超时与截断
- 异常恢复
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from agent_core.bash_runtime import BashRuntime, BashRuntimeConfig

pytestmark = pytest.mark.asyncio


# ── 辅助 ──────────────────────────────────────────────────────

def _make_runtime(**overrides) -> BashRuntime:
    defaults = dict(
        shell_path="/bin/bash",
        base_dir="/tmp",
        default_timeout_seconds=10,
        max_timeout_seconds=30,
        default_output_limit=50_000,
        max_output_limit=200_000,
    )
    defaults.update(overrides)
    return BashRuntime(config=BashRuntimeConfig(**defaults))


# ── 生命周期 ──────────────────────────────────────────────────

class TestLifecycle:
    async def test_start_creates_process(self):
        rt = _make_runtime()
        await rt.start()
        assert rt.is_alive
        assert rt.pid is not None
        await rt.close()

    async def test_close_kills_process(self):
        rt = _make_runtime()
        await rt.start()
        pid = rt.pid
        await rt.close()
        assert not rt.is_alive
        assert rt.pid is None
        # 进程应当已终止
        try:
            os.kill(pid, 0)
            pytest.fail("process still alive after close()")
        except ProcessLookupError:
            pass

    async def test_restart_resets_state(self):
        rt = _make_runtime()
        await rt.start()
        r = await rt.execute("export MYVAR=hello")
        assert r.exit_code == 0
        await rt.restart()
        r2 = await rt.execute("echo $MYVAR")
        assert r2.exit_code == 0
        # restart 后 MYVAR 应不存在
        assert "hello" not in r2.stdout

    async def test_double_close_safe(self):
        rt = _make_runtime()
        await rt.start()
        await rt.close()
        await rt.close()

    async def test_auto_restart_on_dead_process(self):
        rt = _make_runtime()
        await rt.start()
        # 人为杀掉 bash
        rt._process.terminate()
        await asyncio.sleep(0.2)
        assert not rt.is_alive
        # execute 应自动重启
        r = await rt.execute("echo recovered")
        assert r.exit_code == 0
        assert "recovered" in r.stdout
        await rt.close()


# ── 命令执行与 sentinel ───────────────────────────────────────

class TestExecution:
    async def test_simple_echo(self):
        rt = _make_runtime()
        await rt.start()
        r = await rt.execute("echo hello world")
        assert r.exit_code == 0
        assert "hello world" in r.stdout
        assert r.timed_out is False
        assert r.truncated is False
        await rt.close()

    async def test_non_zero_exit(self):
        rt = _make_runtime()
        await rt.start()
        # 用子 shell 测试非零退出码（直接 exit 会杀死整个 bash 进程）
        r = await rt.execute("(exit 42)")
        assert r.exit_code == 42
        await rt.close()

    async def test_exit_kills_bash(self):
        """直接 exit 会终止 bash 进程，exit_code 为 -1（sentinel 未写入）。"""
        rt = _make_runtime()
        await rt.start()
        r = await rt.execute("exit 7")
        assert r.exit_code == -1
        # execute 内部会 wait() reap 已退出的进程
        await asyncio.sleep(0.1)
        assert not rt.is_alive
        # 下次 execute 自动恢复
        r2 = await rt.execute("echo recovered")
        assert r2.exit_code == 0
        assert "recovered" in r2.stdout
        await rt.close()

    async def test_stderr_capture(self):
        rt = _make_runtime()
        await rt.start()
        r = await rt.execute("echo err >&2")
        assert r.exit_code == 0
        assert "err" in r.stderr
        await rt.close()

    async def test_multiline_output(self):
        rt = _make_runtime()
        await rt.start()
        r = await rt.execute("for i in 1 2 3; do echo $i; done")
        assert r.exit_code == 0
        lines = r.stdout.strip().splitlines()
        assert lines == ["1", "2", "3"]
        await rt.close()

    async def test_command_with_special_chars(self):
        rt = _make_runtime()
        await rt.start()
        r = await rt.execute("echo 'hello \"world\"' && echo 'foo=bar'")
        assert r.exit_code == 0
        assert 'hello "world"' in r.stdout
        assert "foo=bar" in r.stdout
        await rt.close()


# ── 会话持久化 ────────────────────────────────────────────────

class TestPersistence:
    async def test_cd_persists(self):
        rt = _make_runtime()
        await rt.start()
        with tempfile.TemporaryDirectory() as tmpdir:
            await rt.execute(f"cd {tmpdir}")
            r = await rt.execute("pwd")
            assert tmpdir in r.stdout
        await rt.close()

    async def test_export_persists(self):
        rt = _make_runtime()
        await rt.start()
        await rt.execute("export MY_TEST_VAR=persistent123")
        r = await rt.execute("echo $MY_TEST_VAR")
        assert "persistent123" in r.stdout
        await rt.close()

    async def test_function_persists(self):
        rt = _make_runtime()
        await rt.start()
        await rt.execute("myfn() { echo fn_output; }")
        r = await rt.execute("myfn")
        assert "fn_output" in r.stdout
        await rt.close()

    async def test_multiple_sequential_commands(self):
        rt = _make_runtime()
        await rt.start()
        for i in range(5):
            r = await rt.execute(f"echo cmd_{i}")
            assert r.exit_code == 0
            assert f"cmd_{i}" in r.stdout
        assert rt.command_count == 5
        await rt.close()


# ── 超时 ──────────────────────────────────────────────────────

class TestTimeout:
    async def test_command_timeout(self):
        rt = _make_runtime(default_timeout_seconds=2.0)
        await rt.start()
        r = await rt.execute("sleep 60", timeout=1.0)
        assert r.timed_out is True
        # 超时后 bash 应自动重启
        r2 = await rt.execute("echo ok")
        assert r2.exit_code == 0
        assert "ok" in r2.stdout
        await rt.close()


# ── 输出截断 ──────────────────────────────────────────────────

class TestTruncation:
    async def test_output_truncated(self):
        rt = _make_runtime(default_output_limit=100)
        await rt.start()
        r = await rt.execute("python3 -c \"print('x' * 500)\"")
        assert r.truncated is True
        assert len(r.stdout) <= 100
        await rt.close()


# ── 初始化命令 ────────────────────────────────────────────────

class TestInitCommands:
    async def test_init_commands_executed(self):
        rt = _make_runtime(init_commands=["export INIT_VAR=from_init"])
        await rt.start()
        r = await rt.execute("echo $INIT_VAR")
        assert "from_init" in r.stdout
        await rt.close()
