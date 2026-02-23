"""
命令执行工具 - 提供 run_command

在受控目录下执行终端命令，支持超时、工作目录、输出长度限制和返回码。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
- 查看目录/文件信息（例如 ls、pwd）
- 运行脚本或测试命令（例如 pytest、python script.py）
- 查询 Git 状态、构建状态等开发信息

工具会：
- 在受控基础目录下执行命令
- 支持自定义工作目录 cwd
- 支持超时 timeout（秒）
- 限制输出长度 output_limit（字符）
- 返回 return_code、stdout、stderr""",
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
                    description="工作目录（相对或绝对路径，必须位于 command_tools.base_dir 内）",
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
            ],
            examples=[
                {
                    "description": "查看当前目录文件",
                    "params": {"command": "ls -la"},
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
                "cwd 必须位于允许的 base_dir 内，越界会被拒绝",
                "超时时会终止进程并返回 COMMAND_TIMEOUT",
                "即使 return_code 非 0，也会返回 stdout/stderr 方便排查",
            ],
            tags=['命令', '执行'],
        )

    def _resolve_cwd(self, cwd: Optional[str]) -> tuple[Optional[Path], Optional[str]]:
        base = Path(self._cmd_config.base_dir).resolve()
        cwd_str = cwd or "."
        try:
            target = (base / cwd_str).resolve()
            if not str(target).startswith(str(base)):
                return None, f"工作目录 '{cwd_str}' 超出允许的目录范围"
            if not target.exists():
                return None, f"工作目录不存在: {cwd_str}"
            if not target.is_dir():
                return None, f"工作目录不是目录: {cwd_str}"
            return target, None
        except (OSError, ValueError) as e:
            return None, f"无效工作目录: {e}"

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
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(resolved_cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
            process.kill()
            await process.wait()
        finally:
            await asyncio.gather(stdout_task, stderr_task)

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
