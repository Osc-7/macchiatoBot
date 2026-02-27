"""
run_command 工具测试
"""

import os

import pytest

from schedule_agent.config import CommandToolsConfig, Config, LLMConfig
from schedule_agent.core.tools.command_tools import RunCommandTool
from schedule_agent.core.tools.dev_terminal_tool import DevTerminalTool


def _make_config(
    *,
    enabled: bool = True,
    allow_run: bool = True,
    base_dir: str = ".",
    default_timeout_seconds: float = 2.0,
    max_timeout_seconds: float = 10.0,
    default_output_limit: int = 2000,
    max_output_limit: int = 5000,
) -> Config:
    return Config(
        llm=LLMConfig(api_key="test", model="test"),
        command_tools=CommandToolsConfig(
            enabled=enabled,
            allow_run=allow_run,
            base_dir=base_dir,
            default_timeout_seconds=default_timeout_seconds,
            max_timeout_seconds=max_timeout_seconds,
            default_output_limit=default_output_limit,
            max_output_limit=max_output_limit,
        ),
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
    async def test_run_command_reject_outside_cwd(self, tmp_path):
        config = _make_config(base_dir=str(tmp_path))
        tool = RunCommandTool(config=config)
        result = await tool.execute(command="pwd", cwd="../")
        assert not result.success
        assert result.error == "INVALID_CWD"

    @pytest.mark.asyncio
    async def test_run_command_output_limit(self, tmp_path):
        config = _make_config(base_dir=str(tmp_path), default_output_limit=20, max_output_limit=20)
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


class TestDevTerminalTool:
    def test_get_definition(self):
        config = _make_config()
        tool = DevTerminalTool(config=config)
        definition = tool.get_definition()
        assert definition.name == "dev_terminal_session"
        param_names = [p.name for p in definition.parameters]
        assert "terminal_name" in param_names
        assert "command" in param_names
        assert "cwd" in param_names
        assert "wait_for_text" in param_names

    @pytest.mark.asyncio
    async def test_dev_terminal_unavailable_returns_error(self, tmp_path, monkeypatch):
        # 使用一个几乎不可能有服务监听的端口，确保连接失败
        monkeypatch.setenv("DEV_TERMINAL_URL", "http://127.0.0.1:9")
        config = _make_config(base_dir=str(tmp_path))
        tool = DevTerminalTool(config=config)
        result = await tool.execute(terminal_name="test-session")
        assert not result.success
        assert result.error == "DEV_TERMINAL_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_dev_terminal_respects_permission_switch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEV_TERMINAL_URL", "http://127.0.0.1:9")
        config = _make_config(enabled=False, base_dir=str(tmp_path))
        tool = DevTerminalTool(config=config)
        result = await tool.execute(terminal_name="test-session")
        assert not result.success
        assert result.error == "PERMISSION_DENIED"
