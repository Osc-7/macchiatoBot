"""
命令执行工具 - 提供 run_command

执行终端命令，支持超时、工作目录、输出长度限制和返回码。
cwd 可指向任意有效路径；危险操作需用户确认（confirm=true）后执行。
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# 超时后给进程退出留的宽限时间（秒）；超时后仍未退出则 SIGKILL
_KILL_GRACE_SECONDS = 5

# 危险命令模式：匹配到且未传 confirm=true 时需用户确认
_DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s+(-[^ ]*r[^ ]*|-rf|-fr|-r\s+-f)\b", re.I),  # rm -rf / rm -r
    re.compile(r"\brm\s+-[^ ]*f[^ ]*\s+-r\b", re.I),
    re.compile(r"\bchmod\s+(-R|--recursive)\b", re.I),
    re.compile(r"\bchown\s+(-R|--recursive)\b", re.I),
    re.compile(r"\bdd\s+", re.I),
    re.compile(r"\bsudo\b", re.I),
    re.compile(r"\bmkfs\.", re.I),
    re.compile(r">\s*/dev/(sd|hd|nvme|vd)[a-z]", re.I),
    re.compile(r"\bformat\s+", re.I),
]

from schedule_agent.config import CommandToolsConfig, Config, get_config

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult


@dataclass
class _OutputCollector:
    """按总字符上限收集 stdout/stderr。"""

    limit: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False

    def _remaining(self) -> int:
        return self.limit - len(self.stdout) - len(self.stderr)

    def append(self, stream: str, chunk: bytes) -> None:
        if not chunk:
            return
        text = chunk.decode("utf-8", errors="replace")
        remaining = self._remaining()
        if remaining <= 0:
            self.truncated = True
            return

        if len(text) > remaining:
            text = text[:remaining]
            self.truncated = True

        if stream == "stdout":
            self.stdout += text
        else:
            self.stderr += text


class RunCommandTool(BaseTool):
    """
    终端命令执行工具。

    支持：
    - command: 要执行的命令
    - timeout: 超时秒数
    - cwd: 工作目录（相对 command_tools.base_dir）
    - output_limit: 输出长度限制（stdout+stderr 总和）
    - return_code: 返回码
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        command_tools_config: Optional[CommandToolsConfig] = None,
    ):
        self._config = config or get_config()
        self._cmd_config = command_tools_config or self._config.command_tools

    @property
    def name(self) -> str:
        return "run_command"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""执行终端命令并返回结果。

当用户想要：
- 查看目录/文件信息（例如 ls、pwd、ls /etc）
- 运行脚本或测试命令（例如 pytest、python script.py）
- 查询 Git 状态、构建状态等开发信息
- 访问系统目录（/etc、/usr、~/.config 等）

工具会：
- 在指定工作目录 cwd 下执行（cwd 可为任意有效路径）
- 支持超时 timeout（秒）
- 限制输出长度 output_limit（字符）
- 危险操作（rm -rf、chmod -R、sudo 等）需用户确认后传 confirm=true""",
            parameters=[
                ToolParameter(
                    name="command",
                    type="string",
                    description="要执行的 shell 命令",
                    required=True,
                ),
                ToolParameter(
                    name="cwd",
                    type="string",
                    description="工作目录（相对路径相对于 base_dir，绝对路径可为任意有效目录如 /etc、~/.config）",
                    required=False,
                    default=".",
                ),
                ToolParameter(
                    name="timeout",
                    type="number",
                    description="超时时间（秒），默认使用配置 command_tools.default_timeout_seconds",
                    required=False,
                ),
                ToolParameter(
                    name="output_limit",
                    type="integer",
                    description="输出长度限制（stdout+stderr 总字符数），默认使用配置 command_tools.default_output_limit",
                    required=False,
                ),
                ToolParameter(
                    name="confirm",
                    type="boolean",
                    description="危险操作需用户过目确认后设为 true（如 rm -rf、chmod -R、sudo 等）",
                    required=False,
                    default=False,
                ),
            ],
            examples=[
                {
                    "description": "查看当前目录文件",
                    "params": {"command": "ls -la"},
                },
                {
                    "description": "查看系统目录",
                    "params": {"command": "ls -la", "cwd": "/etc"},
                },
                {
                    "description": "在指定子目录运行测试，超时 20 秒",
                    "params": {
                        "command": "pytest -q",
                        "cwd": "tests",
                        "timeout": 20,
                    },
                },
            ],
            usage_notes=[
                "cwd 支持任意有效路径（/etc、/usr、~/.config 等），相对路径相对于 base_dir",
                "危险命令（rm -rf、chmod -R、sudo、dd 等）需先向用户展示命令，待用户确认后再传 confirm=true 执行",
                "超时时会终止进程并返回 COMMAND_TIMEOUT",
                "即使 return_code 非 0，也会返回 stdout/stderr 方便排查",
            ],
            tags=['命令', '执行'],
        )

    def _resolve_cwd(self, cwd: Optional[str]) -> tuple[Optional[Path], Optional[str]]:
        """
        解析工作目录。

        支持任意有效路径：相对路径相对于 base_dir，绝对路径直接使用。
        """
        base = Path(self._cmd_config.base_dir).resolve()
        cwd_str = cwd or "."
        try:
            raw_path = Path(cwd_str).expanduser()
            if raw_path.is_absolute():
                target = raw_path.resolve()
            else:
                target = (base / raw_path).resolve()

            if not target.exists():
                return None, f"工作目录不存在: {cwd_str}"
            if not target.is_dir():
                return None, f"工作目录不是目录: {cwd_str}"
            return target, None
        except (OSError, ValueError) as e:
            return None, f"无效工作目录: {e}"

    @staticmethod
    def _is_dangerous_command(command: str) -> bool:
        """检测是否为危险命令（需用户确认）。"""
        for pat in _DANGEROUS_PATTERNS:
            if pat.search(command):
                return True
        return False

    @staticmethod
    async def _read_stream(
        reader: Optional[asyncio.StreamReader],
        stream_name: str,
        collector: _OutputCollector,
    ) -> None:
        if reader is None:
            return
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            collector.append(stream_name, chunk)

    async def execute(self, **kwargs) -> ToolResult:
        command = str(kwargs.get("command", "")).strip()
        if not command:
            return ToolResult(
                success=False,
                error="MISSING_COMMAND",
                message="缺少必需参数: command",
            )

        if not self._cmd_config.enabled or not self._cmd_config.allow_run:
            return ToolResult(
                success=False,
                error="PERMISSION_DENIED",
                message="命令执行功能未启用，请在配置中设置 command_tools.enabled 和 command_tools.allow_run 为 true",
            )

        # 危险命令需用户确认
        if self._is_dangerous_command(command) and kwargs.get("confirm") is not True:
            return ToolResult(
                success=False,
                error="CONFIRMATION_REQUIRED",
                message="该命令可能造成不可逆损害（如 rm -rf、chmod -R、sudo 等）。请先向用户展示命令内容，待用户确认后再调用并传 confirm=true 执行",
            )

        timeout = kwargs.get("timeout", self._cmd_config.default_timeout_seconds)
        output_limit = kwargs.get("output_limit", self._cmd_config.default_output_limit)

        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                error="INVALID_TIMEOUT",
                message="timeout 必须是数字（秒）",
            )

        if timeout <= 0:
            return ToolResult(
                success=False,
                error="INVALID_TIMEOUT",
                message="timeout 必须大于 0",
            )
        if timeout > self._cmd_config.max_timeout_seconds:
            return ToolResult(
                success=False,
                error="TIMEOUT_TOO_LARGE",
                message=f"timeout 超过允许上限 {self._cmd_config.max_timeout_seconds} 秒",
            )

        try:
            output_limit = int(output_limit)
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                error="INVALID_OUTPUT_LIMIT",
                message="output_limit 必须是整数",
            )

        if output_limit <= 0:
            return ToolResult(
                success=False,
                error="INVALID_OUTPUT_LIMIT",
                message="output_limit 必须大于 0",
            )
        if output_limit > self._cmd_config.max_output_limit:
            return ToolResult(
                success=False,
                error="OUTPUT_LIMIT_TOO_LARGE",
                message=f"output_limit 超过允许上限 {self._cmd_config.max_output_limit}",
            )

        resolved_cwd, cwd_err = self._resolve_cwd(kwargs.get("cwd"))
        if cwd_err:
            return ToolResult(success=False, error="INVALID_CWD", message=cwd_err)

        collector = _OutputCollector(limit=output_limit)
        # start_new_session=True 让子进程拥有独立进程组，超时可 killpg 杀死整棵进程树
        # （否则只杀 shell，后台子进程会继承 pipe 写端，read 永无 EOF，gather 无限阻塞）
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(resolved_cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name != "nt"),
        )

        stdout_task = asyncio.create_task(
            self._read_stream(process.stdout, "stdout", collector)
        )
        stderr_task = asyncio.create_task(
            self._read_stream(process.stderr, "stderr", collector)
        )

        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            # 杀死整棵进程树（shell + 子进程如 server.sh、npx），否则子进程继承 pipe
            # 写端不关闭，_read_stream 永无 EOF，gather(stdout_task, stderr_task) 无限阻塞
            if os.name != "nt" and hasattr(signal, "SIGTERM"):
                try:
                    # start_new_session 下 shell 是 session 领导者，PGID == pid
                    os.killpg(process.pid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    process.terminate()
            else:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_KILL_GRACE_SECONDS)
            except asyncio.TimeoutError:
                if os.name != "nt" and hasattr(signal, "SIGKILL"):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        process.kill()
                else:
                    process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=_KILL_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    pass  # 仍不退出则放弃等待
        finally:
            # 若子进程仍持有 pipe 写端，read 可能永不返回；加超时防止无限阻塞
            try:
                await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task),
                    timeout=_KILL_GRACE_SECONDS,
                )
            except asyncio.TimeoutError:
                stdout_task.cancel()
                stderr_task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        return_code = process.returncode
        data = {
            "command": command,
            "cwd": str(resolved_cwd),
            "stdout": collector.stdout,
            "stderr": collector.stderr,
            "return_code": return_code,
            "timeout": timeout,
            "timed_out": timed_out,
            "output_limit": output_limit,
            "truncated": collector.truncated,
        }

        if timed_out:
            return ToolResult(
                success=False,
                data=data,
                error="COMMAND_TIMEOUT",
                message=f"命令执行超时（>{timeout} 秒），进程已终止",
            )

        if return_code == 0:
            return ToolResult(
                success=True,
                data=data,
                message="命令执行成功",
            )

        return ToolResult(
            success=False,
            data=data,
            error="NON_ZERO_EXIT",
            message=f"命令执行结束，返回码为 {return_code}",
        )
