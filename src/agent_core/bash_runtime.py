"""
BashRuntime -- 长驻 bash 进程管理器。

每个 AgentCore 实例持有一个 BashRuntime，生命周期与 Core 同步：
- start(): 启动 bash 子进程
- execute(): 通过 stdin 喂命令、用 sentinel 识别完成、捕获 stdout/stderr/exit_code
- restart(): 杀掉并重建 bash
- close(): 杀掉 bash 并可选写快照

参考：
- Anthropic 官方 Bash tool 文档 (BashSession / Popen 示例)
- OpenAI Codex codex-rs/core/src/spawn.rs (detach_from_tty, parent death signal)
- OpenAI Codex codex-rs/core/src/tools/runtimes/mod.rs (snapshot source + exec)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_SENTINEL_TAG = "__MACCHIATO_BASH_SENTINEL__"
_ERR_SENTINEL_TAG = "__MACCHIATO_BASH_ERR_SENTINEL__"
_KILL_GRACE_SECONDS = 3


@dataclass
class BashResult:
    """单条命令的执行结果。"""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    timed_out: bool = False
    truncated: bool = False
    command: str = ""


@dataclass
class BashRuntimeConfig:
    """BashRuntime 所需的配置子集。"""

    shell_path: str = "/bin/bash"
    base_dir: str = "."
    default_timeout_seconds: float = 30.0
    max_timeout_seconds: float = 300.0
    default_output_limit: int = 12_000
    max_output_limit: int = 200_000
    init_commands: list[str] = field(default_factory=list)
    snapshot_enabled: bool = False
    snapshot_dir: str = "./data/bash_snapshots"
    # 非空时以 ``runuser_path -u <user> -- <shell>`` 启动 bash（Linux 降权）
    run_as_user: Optional[str] = None
    runuser_path: str = "/sbin/runuser"
    # 若设置则作为子进程完整环境（不继承 os.environ）；用于 runuser 场景避免泄露宿主 env
    subprocess_env: Optional[Dict[str, str]] = None


class BashRuntime:
    """
    长驻 bash 子进程。

    通过 stdin pipe 喂命令，用唯一 sentinel 标记命令结束并提取 exit code。
    stdout/stderr 分别收集，支持 output_limit 截断。
    """

    def __init__(self, config: Optional[BashRuntimeConfig] = None) -> None:
        self._config = config or BashRuntimeConfig()
        self._process: Optional[asyncio.subprocess.Process] = None
        self._started = False
        self._command_count = 0

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self, *, snapshot_path: Optional[Path] = None) -> None:
        """启动 bash 子进程。若提供 snapshot 则先 source 之。"""
        if self._started and self.is_alive:
            return

        base_dir = Path(self._config.base_dir).resolve()
        if not base_dir.is_dir():
            base_dir = Path.cwd()

        env: Dict[str, str]
        if self._config.subprocess_env is not None:
            env = dict(self._config.subprocess_env)
        else:
            env = {**os.environ, "MACCHIATO_BASH": "1"}

        run_as = (self._config.run_as_user or "").strip()
        if run_as:
            cmd: tuple[str, ...] = (
                self._config.runuser_path,
                "-u",
                run_as,
                "--",
                self._config.shell_path,
                "--norc",
                "--noprofile",
            )
        else:
            cmd = (self._config.shell_path, "--norc", "--noprofile")

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(base_dir),
            start_new_session=(os.name != "nt"),
            env=env,
        )
        self._started = True
        self._command_count = 0
        logger.info(
            "BashRuntime started: pid=%s shell=%s cwd=%s run_as=%s",
            self._process.pid,
            self._config.shell_path,
            base_dir,
            run_as or "-",
        )

        if snapshot_path and snapshot_path.is_file():
            await self._raw_write(f". '{snapshot_path}' >/dev/null 2>&1 || true\n")

        for cmd in self._config.init_commands:
            await self._raw_write(cmd.rstrip("\n") + "\n")

    async def restart(self) -> None:
        """杀掉当前 bash 并重新启动。"""
        await self.close(write_snapshot=False)
        await self.start()

    async def close(self, *, write_snapshot: bool = False) -> None:
        """终止 bash 子进程，可选写快照。"""
        if self._process is None:
            return

        if write_snapshot and self._config.snapshot_enabled and self.is_alive:
            try:
                await self._write_snapshot()
            except Exception:
                logger.warning("BashRuntime: snapshot write failed", exc_info=True)

        await self._kill_process()
        self._started = False
        logger.info("BashRuntime closed: command_count=%d", self._command_count)

    # ── 命令执行 ──────────────────────────────────────────────

    async def execute(
        self,
        command: str,
        *,
        timeout: Optional[float] = None,
        output_limit: Optional[int] = None,
    ) -> BashResult:
        """
        在长驻 bash 中执行一条命令。

        通过 sentinel 机制识别命令完成、提取 exit code。
        支持超时与输出截断。
        """
        if not self.is_alive:
            await self.start()

        timeout = self._clamp_timeout(timeout)
        output_limit = self._clamp_output_limit(output_limit)

        sentinel_id = uuid.uuid4().hex[:12]
        stdout_sentinel = f"{_SENTINEL_TAG}:{sentinel_id}:"
        stderr_sentinel = f"{_ERR_SENTINEL_TAG}:{sentinel_id}"

        wrapped = self._wrap_command(command, sentinel_id)
        await self._raw_write(wrapped)

        self._command_count += 1

        stdout_buf: list[str] = []
        stderr_buf: list[str] = []
        exit_code = -1
        timed_out = False
        truncated = False
        total_chars = 0

        async def _read_stdout() -> int:
            nonlocal truncated, total_chars
            assert self._process and self._process.stdout
            ec = -1
            while True:
                line_bytes = await self._process.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace")
                if line.startswith(stdout_sentinel):
                    parts = line.strip().split(":")
                    try:
                        ec = int(parts[-1])
                    except (ValueError, IndexError):
                        ec = -1
                    break
                if total_chars < output_limit:
                    remaining = output_limit - total_chars
                    if len(line) > remaining:
                        stdout_buf.append(line[:remaining])
                        truncated = True
                    else:
                        stdout_buf.append(line)
                    total_chars += len(line)
                else:
                    truncated = True
            return ec

        async def _read_stderr() -> None:
            nonlocal truncated, total_chars
            assert self._process and self._process.stderr
            while True:
                line_bytes = await self._process.stderr.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace")
                if stderr_sentinel in line:
                    break
                if total_chars < output_limit:
                    remaining = output_limit - total_chars
                    if len(line) > remaining:
                        stderr_buf.append(line[:remaining])
                        truncated = True
                    else:
                        stderr_buf.append(line)
                    total_chars += len(line)
                else:
                    truncated = True

        try:
            results = await asyncio.wait_for(
                asyncio.gather(_read_stdout(), _read_stderr()),
                timeout=timeout,
            )
            exit_code = results[0]
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(
                "BashRuntime: command timed out after %.1fs: %s",
                timeout,
                command[:120],
            )
            # 命令超时后需要重启 bash，因为上一条命令可能仍在运行
            await self.restart()

        # 检测 bash 是否在命令执行期间退出（如 `exit N`）
        if exit_code == -1 and not timed_out and self._process is not None:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

        return BashResult(
            stdout="".join(stdout_buf),
            stderr="".join(stderr_buf),
            exit_code=exit_code,
            timed_out=timed_out,
            truncated=truncated,
            command=command,
        )

    # ── 属性 ──────────────────────────────────────────────────

    @property
    def is_alive(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
        )

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self._process else None

    @property
    def command_count(self) -> int:
        return self._command_count

    # ── 内部方法 ──────────────────────────────────────────────

    def _wrap_command(self, command: str, sentinel_id: str) -> str:
        """将用户命令包装为带 sentinel 的脚本片段。"""
        stdout_sentinel = f"{_SENTINEL_TAG}:{sentinel_id}:"
        stderr_sentinel = f"{_ERR_SENTINEL_TAG}:{sentinel_id}"
        return (
            f"{command}\n"
            f"__MACCHIATO_EC=$?\n"
            f"echo '{stderr_sentinel}' >&2\n"
            f"echo '{stdout_sentinel}'\"$__MACCHIATO_EC\"\n"
        )

    async def _raw_write(self, text: str) -> None:
        """向 bash stdin 写入文本。"""
        if self._process and self._process.stdin:
            self._process.stdin.write(text.encode("utf-8"))
            await self._process.stdin.drain()

    async def _kill_process(self) -> None:
        """终止 bash 进程树。"""
        proc = self._process
        if proc is None:
            return
        self._process = None

        if proc.returncode is not None:
            return

        if os.name != "nt" and hasattr(signal, "SIGTERM"):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                try:
                    proc.terminate()
                except ProcessLookupError:
                    return
        else:
            try:
                proc.terminate()
            except ProcessLookupError:
                return

        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            if os.name != "nt" and hasattr(signal, "SIGKILL"):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            else:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _write_snapshot(self) -> None:
        """将当前 bash 环境导出为快照脚本（env + cwd）。"""
        snap_dir = Path(self._config.snapshot_dir)
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap_path = snap_dir / f"snapshot_{uuid.uuid4().hex[:8]}.sh"

        result = await self.execute(
            "echo \"__CWD__=$(pwd)\"; env",
            timeout=5,
            output_limit=50_000,
        )
        if result.exit_code != 0:
            return

        lines = []
        cwd = None
        for line in result.stdout.splitlines():
            if line.startswith("__CWD__="):
                cwd = line[len("__CWD__="):]
                continue
            if "=" in line:
                key = line.split("=", 1)[0]
                if key.isidentifier() and key not in (
                    "MACCHIATO_BASH", "SHLVL", "PWD", "OLDPWD", "_",
                ):
                    lines.append(f"export {line}")

        if cwd:
            lines.append(f"cd '{cwd}' 2>/dev/null || true")

        snap_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.debug("BashRuntime: snapshot written to %s", snap_path)

    def _clamp_timeout(self, timeout: Optional[float]) -> float:
        if timeout is None:
            return self._config.default_timeout_seconds
        return max(0.1, min(timeout, self._config.max_timeout_seconds))

    def _clamp_output_limit(self, limit: Optional[int]) -> int:
        if limit is None:
            return self._config.default_output_limit
        return max(1, min(limit, self._config.max_output_limit))
