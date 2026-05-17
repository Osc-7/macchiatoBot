"""Remote worker shell runtime tests."""

from __future__ import annotations

import pytest

from macchiato_remote.runtime.shell import LocalShellConfig, LocalShellSession

pytestmark = pytest.mark.asyncio


async def test_long_single_line_stdout_is_truncated_without_disconnect(tmp_path):
    session = LocalShellSession(
        LocalShellConfig(root=tmp_path, default_output_limit=100)
    )
    await session.start()
    try:
        result = await session.execute(
            request_id="long-stdout",
            command="head -c 70000 /dev/zero | tr '\\0' x; echo",
            timeout_seconds=5,
        )

        assert result.exit_code == 0
        assert result.truncated is True
        assert len(result.stdout) <= 100
        assert result.stdout == "x" * 100

        follow_up = await session.execute(
            request_id="after-long-stdout",
            command="echo still-alive",
            timeout_seconds=5,
        )
        assert follow_up.exit_code == 0
        assert "still-alive" in follow_up.stdout
    finally:
        await session.close()


async def test_long_single_line_stderr_is_truncated_without_disconnect(tmp_path):
    session = LocalShellSession(
        LocalShellConfig(root=tmp_path, default_output_limit=100)
    )
    await session.start()
    try:
        result = await session.execute(
            request_id="long-stderr",
            command="head -c 70000 /dev/zero | tr '\\0' e >&2",
            timeout_seconds=5,
        )

        assert result.exit_code == 0
        assert result.truncated is True
        assert len(result.stderr) <= 100
        assert result.stderr == "e" * 100

        follow_up = await session.execute(
            request_id="after-long-stderr",
            command="echo still-alive",
            timeout_seconds=5,
        )
        assert follow_up.exit_code == 0
        assert "still-alive" in follow_up.stdout
    finally:
        await session.close()
