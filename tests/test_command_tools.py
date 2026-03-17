"""
run_command 工具测试
"""

import pytest

from typing import Optional

from agent_core.config import CommandToolsConfig, Config, LLMConfig
from agent_core.tools.command_tools import RunCommandTool


def _make_config(
    *,
    enabled: bool = True,
    allow_run: bool = True,
    allow_run_for_subagent: bool = False,
    subagent_command_whitelist: Optional[list] = None,
    base_dir: str = ".",
    default_timeout_seconds: float = 2.0,
    max_timeout_seconds: float = 10.0,
    default_output_limit: int = 2000,
    max_output_limit: int = 5000,
) -> Config:
    cmd = CommandToolsConfig(
        enabled=enabled,
        allow_run=allow_run,
        allow_run_for_subagent=allow_run_for_subagent,
        subagent_command_whitelist=subagent_command_whitelist
        if subagent_command_whitelist is not None
        else ["ls", "pwd", "cat", "head", "tail", "grep", "find", "echo"],
        base_dir=base_dir,
        default_timeout_seconds=default_timeout_seconds,
        max_timeout_seconds=max_timeout_seconds,
        default_output_limit=default_output_limit,
        max_output_limit=max_output_limit,
    )
    return Config(
        llm=LLMConfig(api_key="test", model="test"),
        command_tools=cmd,
    )


class TestRunCommandTool:
    def test_get_definition(self):
        config = _make_config()
        tool = RunCommandTool(config=config)
        definition = tool.get_definition()
        assert definition.name == "run_command"
        param_names = [p.name for p in definition.parameters]
        assert "command" in param_names
        assert "cwd" in param_names
        assert "timeout" in param_names
        assert "output_limit" in param_names
        assert "confirm" in param_names

    @pytest.mark.asyncio
    async def test_run_command_success(self, tmp_path):
        config = _make_config(base_dir=str(tmp_path))
        tool = RunCommandTool(config=config)
        result = await tool.execute(command="echo hello")
        assert result.success
        assert result.data["return_code"] == 0
        assert "hello" in result.data["stdout"]
        assert result.data["timed_out"] is False

    @pytest.mark.asyncio
    async def test_run_command_non_zero_exit(self, tmp_path):
        config = _make_config(base_dir=str(tmp_path))
        tool = RunCommandTool(config=config)
        result = await tool.execute(command="bash -lc 'exit 3'")
        assert not result.success
        assert result.error == "NON_ZERO_EXIT"
        assert result.data["return_code"] == 3

    @pytest.mark.asyncio
    async def test_run_command_timeout(self, tmp_path):
        config = _make_config(base_dir=str(tmp_path), max_timeout_seconds=5.0)
        tool = RunCommandTool(config=config)
        result = await tool.execute(command="sleep 2", timeout=0.2)
        assert not result.success
        assert result.error == "COMMAND_TIMEOUT"
        assert result.data["timed_out"] is True

    @pytest.mark.asyncio
    async def test_run_command_with_cwd(self, tmp_path):
        child = tmp_path / "subdir"
        child.mkdir()
        config = _make_config(base_dir=str(tmp_path))
        tool = RunCommandTool(config=config)
        result = await tool.execute(command="pwd", cwd="subdir")
        assert result.success
        assert str(child) in result.data["stdout"]
        assert result.data["cwd"] == str(child)

    @pytest.mark.asyncio
    async def test_run_command_allow_any_cwd(self, tmp_path):
        """cwd 可为任意有效路径（如 /tmp、系统目录）"""
        config = _make_config(base_dir=str(tmp_path))
        tool = RunCommandTool(config=config)
        result = await tool.execute(command="pwd", cwd="/tmp")
        assert result.success
        assert "/tmp" in result.data["stdout"]

    @pytest.mark.asyncio
    async def test_run_command_dangerous_requires_confirm(self, tmp_path):
        """危险命令未传 confirm 时返回 CONFIRMATION_REQUIRED"""
        config = _make_config(base_dir=str(tmp_path))
        tool = RunCommandTool(config=config)
        result = await tool.execute(command="rm -rf /tmp/some-dir")
        assert not result.success
        assert result.error == "CONFIRMATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_run_command_dangerous_with_confirm(self, tmp_path):
        """危险命令传 confirm=true 后正常执行"""
        config = _make_config(base_dir=str(tmp_path))
        tool = RunCommandTool(config=config)
        # rm -rf 不存在的目录会成功（exit 0）
        result = await tool.execute(
            command="rm -rf /tmp/nonexistent-dir-xyz", confirm=True
        )
        assert result.success

    @pytest.mark.asyncio
    async def test_run_command_output_limit(self, tmp_path):
        config = _make_config(
            base_dir=str(tmp_path), default_output_limit=20, max_output_limit=20
        )
        tool = RunCommandTool(config=config)
        result = await tool.execute(command="python -c \"print('x'*200)\"")
        assert result.data["truncated"] is True
        combined = result.data["stdout"] + result.data["stderr"]
        assert len(combined) <= 20

    @pytest.mark.asyncio
    async def test_run_command_disabled(self, tmp_path):
        config = _make_config(enabled=False, base_dir=str(tmp_path))
        tool = RunCommandTool(config=config)
        result = await tool.execute(command="echo hi")
        assert not result.success
        assert result.error == "PERMISSION_DENIED"

    @pytest.mark.asyncio
    async def test_run_command_sub_mode_denied(self, tmp_path):
        """sub 模式下默认禁止 run_command"""
        config = _make_config(base_dir=str(tmp_path))
        tool = RunCommandTool(config=config)
        result = await tool.execute(
            command="echo hi",
            __execution_context__={"tool_mode": "sub", "source": "shuiyuan"},
        )
        assert not result.success
        assert result.error == "PERMISSION_DENIED"
        assert "allow_run_for_subagent" in result.message

    @pytest.mark.asyncio
    async def test_run_command_sub_mode_allowed(self, tmp_path):
        """sub 模式下 allow_run_for_subagent 开启后白名单内命令可执行"""
        config = _make_config(base_dir=str(tmp_path), allow_run_for_subagent=True)
        tool = RunCommandTool(config=config)
        result = await tool.execute(
            command="echo ok",
            __execution_context__={"tool_mode": "sub", "source": "shuiyuan"},
        )
        assert result.success
        assert "ok" in result.data["stdout"]

    @pytest.mark.asyncio
    async def test_run_command_sub_mode_not_whitelisted(self, tmp_path):
        """sub 模式下非白名单命令拒绝"""
        config = _make_config(base_dir=str(tmp_path), allow_run_for_subagent=True)
        tool = RunCommandTool(config=config)
        result = await tool.execute(
            command="rm -rf /tmp/test",
            __execution_context__={"tool_mode": "sub", "source": "shuiyuan"},
        )
        assert not result.success
        assert result.error == "COMMAND_NOT_WHITELISTED"
        assert "白名单" in result.message

    @pytest.mark.asyncio
    async def test_run_command_sub_mode_shell_operator_denied(self, tmp_path):
        """sub 模式下禁止管道等 shell 运算符"""
        config = _make_config(base_dir=str(tmp_path), allow_run_for_subagent=True)
        tool = RunCommandTool(config=config)
        result = await tool.execute(
            command="ls | grep x",
            __execution_context__={"tool_mode": "sub", "source": "shuiyuan"},
        )
        assert not result.success
        assert result.error == "COMMAND_NOT_WHITELISTED"
        assert "|" in result.message or "shell" in result.message

    @pytest.mark.asyncio
    async def test_run_command_kernel_mode_no_context(self, tmp_path):
        """kernel mode 或无 context 时按原逻辑执行"""
        config = _make_config(base_dir=str(tmp_path))
        tool = RunCommandTool(config=config)
        # 不传 __execution_context__（如 MCP、automation 调用）
        result = await tool.execute(command="echo hi")
        assert result.success
        assert "hi" in result.data["stdout"]

    @pytest.mark.asyncio
    async def test_run_command_subagent_denied_by_default(self, tmp_path):
        """subagent 默认不允许 run_command（需配置 allow_run_for_subagent）"""
        config = _make_config(base_dir=str(tmp_path))
        tool = RunCommandTool(config=config)
        result = await tool.execute(
            command="ls -la",
            __execution_context__={"source": "subagent", "tool_mode": "kernel"},
        )
        assert not result.success
        assert result.error == "PERMISSION_DENIED"
        assert "allow_run_for_subagent" in result.message

    @pytest.mark.asyncio
    async def test_run_command_subagent_allowed_whitelist(self, tmp_path):
        """subagent 开启后仅允许白名单内命令"""
        config = _make_config(
            base_dir=str(tmp_path),
            allow_run_for_subagent=True,
            subagent_command_whitelist=["ls", "pwd", "echo", "find"],
        )
        tool = RunCommandTool(config=config)
        result = await tool.execute(
            command="echo ok",
            __execution_context__={"source": "subagent", "tool_mode": "kernel"},
        )
        assert result.success
        assert "ok" in result.data["stdout"]

    @pytest.mark.asyncio
    async def test_run_command_subagent_find_allowed(self, tmp_path):
        """subagent 白名单含 find 时可执行单条 find 命令"""
        config = _make_config(
            base_dir=str(tmp_path),
            allow_run_for_subagent=True,
            subagent_command_whitelist=["ls", "find", "echo"],
        )
        tool = RunCommandTool(config=config)
        result = await tool.execute(
            command="find . -maxdepth 1 -type f",
            __execution_context__={"source": "subagent", "tool_mode": "kernel"},
        )
        assert result.success

    @pytest.mark.asyncio
    async def test_run_command_subagent_pipe_denied(self, tmp_path):
        """subagent 禁止管道等 shell 运算符"""
        config = _make_config(
            base_dir=str(tmp_path),
            allow_run_for_subagent=True,
        )
        tool = RunCommandTool(config=config)
        result = await tool.execute(
            command="ls | head -5",
            __execution_context__={"source": "subagent", "tool_mode": "kernel"},
        )
        assert not result.success
        assert result.error == "COMMAND_NOT_WHITELISTED"
        assert "|" in result.message or "shell" in result.message

    @pytest.mark.asyncio
    async def test_run_command_subagent_dangerous_denied(self, tmp_path):
        """subagent 禁止危险命令（不可 confirm 绕过）"""
        config = _make_config(
            base_dir=str(tmp_path),
            allow_run_for_subagent=True,
            subagent_command_whitelist=["ls", "rm"],
        )
        tool = RunCommandTool(config=config)
        result = await tool.execute(
            command="rm -rf /tmp/foo",
            __execution_context__={"source": "subagent", "tool_mode": "kernel"},
        )
        assert not result.success
        assert result.error == "COMMAND_DENIED"
        assert "危险" in result.message
