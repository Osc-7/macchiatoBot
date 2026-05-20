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
import codecs
import logging
import os
import shlex
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_SENTINEL_TAG = "__MACCHIATO_BASH_SENTINEL__"
_ERR_SENTINEL_TAG = "__MACCHIATO_BASH_ERR_SENTINEL__"
_KILL_GRACE_SECONDS = 3
_STREAM_READ_CHUNK_SIZE = 4096


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
class BashSessionCapture:
    """从长驻 bash 捕获的工作目录与环境变量（供后台 job 对齐会话）。"""

    cwd: str
    env: Dict[str, str]


_SKIP_ENV_KEYS = frozenset({"MACCHIATO_BASH", "SHLVL", "PWD", "OLDPWD", "_"})


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
    snapshot_enabled: bool = True
    snapshot_dir: str = "./data/bash_snapshots"
    snapshot_keep_count: int = 3
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
        # 每次成功执行后更新；超时重启时恢复（避免在卡死的 shell 里 capture）
        self._last_snapshot_path: Optional[Path] = None

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

    async def restart(self, *, force_snapshot: bool = False) -> None:
        """杀掉当前 bash 并重新启动，并按需恢复环境快照。"""
        snap_path: Optional[Path] = None
        if force_snapshot:
            snap_path = self._last_snapshot_path
        elif self._config.snapshot_enabled and self.is_alive:
            try:
                snap_path = await self._write_snapshot()
            except Exception:
                logger.warning("BashRuntime: snapshot before restart failed", exc_info=True)
        await self.close(write_snapshot=False)
        await self.start(snapshot_path=snap_path)

    async def close(self, *, write_snapshot: bool = False) -> Optional[Path]:
        """终止 bash 子进程，可选写快照。返回 snapshot 路径（若写了）。"""
        if self._process is None:
            return None

        snap_path: Optional[Path] = None
        if write_snapshot and self._config.snapshot_enabled and self.is_alive:
            try:
                snap_path = await self._write_snapshot()
            except Exception:
                logger.warning("BashRuntime: snapshot write failed", exc_info=True)

        await self._kill_process()
        self._started = False
        logger.info("BashRuntime closed: command_count=%d", self._command_count)
        return snap_path

    # ── 命令执行 ──────────────────────────────────────────────

    async def execute(
        self,
        command: str,
        *,
        timeout: Optional[float] = None,
        output_limit: Optional[int] = None,
        restart_on_timeout: bool = True,
        record_snapshot: bool = True,
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

        if record_snapshot:
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
            marker = stdout_sentinel.encode("utf-8")
            tail = await _read_until_marker(self._process.stdout, marker, stdout_buf)
            while b"\n" not in tail:
                chunk = await self._process.stdout.read(_STREAM_READ_CHUNK_SIZE)
                if not chunk:
                    break
                tail += chunk
            try:
                code_text = tail.split(b"\n", 1)[0].decode("ascii", errors="replace")
                return int(code_text.strip())
            except (ValueError, IndexError):
                return -1

        async def _read_stderr() -> None:
            nonlocal truncated, total_chars
            assert self._process and self._process.stderr
            marker = stderr_sentinel.encode("utf-8")
            await _read_until_marker(self._process.stderr, marker, stderr_buf)

        async def _read_until_marker(
            reader: asyncio.StreamReader,
            marker: bytes,
            buf: list[str],
        ) -> bytes:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            pending = b""
            keep = max(0, len(marker) - 1)

            async def _append_bytes(data: bytes, *, final: bool = False) -> None:
                nonlocal total_chars, truncated
                text = decoder.decode(data, final=final)
                if not text:
                    return
                if total_chars >= output_limit:
                    truncated = True
                    return
                remaining = output_limit - total_chars
                if len(text) > remaining:
                    buf.append(text[:remaining])
                    total_chars += remaining
                    truncated = True
                else:
                    buf.append(text)
                    total_chars += len(text)

            while True:
                chunk = await reader.read(_STREAM_READ_CHUNK_SIZE)
                if not chunk:
                    if pending:
                        await _append_bytes(pending, final=True)
                    return b""

                pending += chunk
                marker_index = pending.find(marker)
                if marker_index >= 0:
                    await _append_bytes(pending[:marker_index], final=True)
                    return pending[marker_index + len(marker) :]

                if keep and len(pending) > keep:
                    emit = pending[:-keep]
                    pending = pending[-keep:]
                    await _append_bytes(emit)
                elif not keep:
                    await _append_bytes(pending)
                    pending = b""

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
            if restart_on_timeout:
                snap = self._last_snapshot_path
                await self.close(write_snapshot=False)
                await self.start(snapshot_path=snap)

        # 检测 bash 是否在命令执行期间退出（如 `exit N`）
        if exit_code == -1 and not timed_out and self._process is not None:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

        result = BashResult(
            stdout="".join(stdout_buf),
            stderr="".join(stderr_buf),
            exit_code=exit_code,
            timed_out=timed_out,
            truncated=truncated,
            command=command,
        )
        if (
            restart_on_timeout
            and record_snapshot
            and not timed_out
            and self.is_alive
            and exit_code >= 0
        ):
            try:
                self._last_snapshot_path = await self._write_snapshot()
            except Exception:
                logger.debug(
                    "BashRuntime: post-command snapshot failed",
                    exc_info=True,
                )
        return result

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

    async def capture_session(self) -> Optional[BashSessionCapture]:
        """捕获当前 bash 的 cwd 与环境变量（失败时返回 None）。"""
        if not self.is_alive:
            return None
        result = await self.execute(
            'echo "__CWD__=$(pwd)"; env',
            timeout=5,
            output_limit=50_000,
            restart_on_timeout=False,
            record_snapshot=False,
        )
        if result.exit_code != 0:
            return None
        cwd, env = self._parse_cwd_and_env(result.stdout)
        if not cwd:
            cwd = str(Path(self._config.base_dir).resolve())
        return BashSessionCapture(cwd=cwd, env=env)

    @staticmethod
    def _parse_cwd_and_env(stdout: str) -> tuple[Optional[str], Dict[str, str]]:
        cwd: Optional[str] = None
        env: Dict[str, str] = {}
        for line in stdout.splitlines():
            if line.startswith("__CWD__="):
                cwd = line[len("__CWD__=") :].strip()
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.isidentifier() and key not in _SKIP_ENV_KEYS:
                env[key] = value
        return cwd, env

    async def _write_snapshot(self) -> Optional[Path]:
        """将当前 bash 环境导出为快照脚本（env + cwd），返回文件路径。"""
        snap_dir = Path(self._config.snapshot_dir)
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap_path = snap_dir / f"snapshot_{uuid.uuid4().hex[:8]}.sh"

        capture = await self.capture_session()
        if capture is None:
            return None

        lines = [
            f"export {k}={shlex.quote(v)}" for k, v in sorted(capture.env.items())
        ]
        lines.append(f"cd {shlex.quote(capture.cwd)} 2>/dev/null || true")

        snap_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.debug("BashRuntime: snapshot written to %s", snap_path)

        self._prune_snapshots(snap_dir)
        return snap_path

    def _prune_snapshots(self, snap_dir: Path) -> None:
        """仅保留最近 N 个 snapshot，防止磁盘膨胀。"""
        keep = max(1, self._config.snapshot_keep_count)
        try:
            files = sorted(
                (p for p in snap_dir.iterdir() if p.is_file() and p.name.startswith("snapshot_")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old in files[keep:]:
                try:
                    old.unlink()
                    logger.debug("BashRuntime: removed old snapshot %s", old)
                except OSError:
                    pass
        except OSError:
            pass

    def _clamp_timeout(self, timeout: Optional[float]) -> float:
        if timeout is None:
            return self._config.default_timeout_seconds
        return max(0.1, min(timeout, self._config.max_timeout_seconds))

    def _clamp_output_limit(self, limit: Optional[int]) -> int:
        if limit is None:
            return self._config.default_output_limit
        return max(1, min(limit, self._config.max_output_limit))
