"""Persistent local shell sessions for macchiato-remote."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from macchiato_remote.protocol import RemoteCommandResult

_SENTINEL_TAG = "__MACCHIATO_REMOTE_SENTINEL__"
_ERR_SENTINEL_TAG = "__MACCHIATO_REMOTE_ERR_SENTINEL__"


@dataclass
class LocalShellConfig:
    root: Path
    shell_path: str = "/bin/bash"
    default_timeout_seconds: float = 30.0
    default_output_limit: int = 12000


class LocalShellSession:
    """A long-lived shell rooted at a user-authorized local directory."""

    def __init__(self, config: LocalShellConfig) -> None:
        self._config = config
        self._process: Optional[asyncio.subprocess.Process] = None

    @property
    def root(self) -> Path:
        return self._config.root

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        if self.is_alive:
            return
        root = self._config.root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"remote workspace path is not a directory: {root}")
        env = {
            **os.environ,
            "HOME": str(root),
            "MACCHIATO_REMOTE_WORKSPACE": str(root),
            "MACCHIATO_WORKSPACE_ROOT": str(root),
            "MACCHIATO_REMOTE": "1",
        }
        self._process = await asyncio.create_subprocess_exec(
            self._config.shell_path,
            "--norc",
            "--noprofile",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(root),
            env=env,
            start_new_session=(os.name != "nt"),
        )

    async def close(self) -> None:
        proc = self._process
        self._process = None
        if proc is None or proc.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except (OSError, ProcessLookupError):
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except (OSError, ProcessLookupError):
                pass

    async def execute(
        self,
        *,
        request_id: str,
        command: str,
        timeout_seconds: Optional[float] = None,
        output_limit: Optional[int] = None,
    ) -> RemoteCommandResult:
        if not self.is_alive:
            await self.start()

        timeout = float(timeout_seconds or self._config.default_timeout_seconds)
        limit = int(output_limit or self._config.default_output_limit)
        rewritten = self._rewrite_virtual_paths(command)
        sentinel_id = uuid.uuid4().hex[:12]
        stdout_sentinel = f"{_SENTINEL_TAG}:{sentinel_id}:"
        stderr_sentinel = f"{_ERR_SENTINEL_TAG}:{sentinel_id}"

        wrapped = (
            f"{rewritten}\n"
            f"__MACCHIATO_REMOTE_EC=$?\n"
            f"echo '{stderr_sentinel}' >&2\n"
            f"echo '{stdout_sentinel}'\"$__MACCHIATO_REMOTE_EC\"\n"
        )

        assert self._process is not None and self._process.stdin is not None
        self._process.stdin.write(wrapped.encode("utf-8"))
        await self._process.stdin.drain()

        stdout_buf: list[str] = []
        stderr_buf: list[str] = []
        total_chars = 0
        truncated = False
        exit_code = -1

        async def _append(buf: list[str], line: str) -> None:
            nonlocal total_chars, truncated
            if total_chars >= limit:
                truncated = True
                return
            remaining = limit - total_chars
            if len(line) > remaining:
                buf.append(line[:remaining])
                total_chars += remaining
                truncated = True
            else:
                buf.append(line)
                total_chars += len(line)

        async def _read_stdout() -> int:
            assert self._process is not None and self._process.stdout is not None
            ec = -1
            while True:
                raw = await self._process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace")
                if line.startswith(stdout_sentinel):
                    try:
                        ec = int(line.strip().split(":")[-1])
                    except (ValueError, IndexError):
                        ec = -1
                    break
                await _append(stdout_buf, line)
            return ec

        async def _read_stderr() -> None:
            assert self._process is not None and self._process.stderr is not None
            while True:
                raw = await self._process.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace")
                if stderr_sentinel in line:
                    break
                await _append(stderr_buf, line)

        timed_out = False
        try:
            results = await asyncio.wait_for(
                asyncio.gather(_read_stdout(), _read_stderr()),
                timeout=timeout,
            )
            exit_code = int(results[0])
        except asyncio.TimeoutError:
            timed_out = True
            await self.close()

        return RemoteCommandResult(
            request_id=request_id,
            command=command,
            stdout="".join(stdout_buf),
            stderr="".join(stderr_buf),
            exit_code=exit_code,
            timed_out=timed_out,
            truncated=truncated,
            cwd=str(self.root),
        )

    def _rewrite_virtual_paths(self, command: str) -> str:
        """Best-effort fallback for hosts without a real /workspace mount.

        A proper Linux sandbox can bind the authorized directory at /workspace.
        This fallback keeps the MVP useful on macOS and plain shells by mapping
        common unquoted /workspace references to the resolved local root.
        """
        root_q = shlex.quote(str(self.root))
        return command.replace("/workspace", root_q)
