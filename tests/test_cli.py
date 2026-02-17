"""
CLI 模块测试

测试命令行交互界面的功能。
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from io import StringIO
import sys

from schedule_agent.config import Config, LLMConfig
from schedule_agent.core import ScheduleAgent
from schedule_agent.core.tools import BaseTool

# 导入 main 模块的函数
import main as cli_module


class TestGetDefaultTools:
    """测试获取默认工具功能"""

    def test_get_default_tools_returns_list(self):
        """测试返回工具列表"""
        tools = cli_module.get_default_tools()
        assert isinstance(tools, list)
        assert len(tools) > 0

    def test_get_default_tools_all_are_base_tool(self):
        """测试所有工具都是 BaseTool 实例"""
        tools = cli_module.get_default_tools()
        for tool in tools:
            assert isinstance(tool, BaseTool)

    def test_get_default_tools_contains_expected_tools(self):
        """测试包含预期的工具"""
        tools = cli_module.get_default_tools()
        tool_names = [t.name for t in tools]

        expected_tools = [
            "parse_time",
            "add_event",
            "add_task",
            "get_events",
            "get_tasks",
            "get_free_slots",
            "plan_tasks",
        ]

        for expected in expected_tools:
            assert expected in tool_names, f"缺少工具: {expected}"


class TestPrintFunctions:
    """测试打印函数"""

    def test_print_welcome(self, capsys):
        """测试打印欢迎信息"""
        cli_module.print_welcome()
        captured = capsys.readouterr()

        assert "Schedule Agent" in captured.out
        assert "日程管理助手" in captured.out
        assert "quit" in captured.out
        assert "exit" in captured.out
        assert "clear" in captured.out
        assert "help" in captured.out

    def test_print_help(self, capsys):
        """测试打印帮助信息"""
        cli_module.print_help()
        captured = capsys.readouterr()

        assert "帮助信息" in captured.out
        assert "quit" in captured.out
        assert "clear" in captured.out
        assert "help" in captured.out
        assert "示例对话" in captured.out


class TestRunSingleCommand:
    """测试单条命令执行"""

    @pytest.fixture
    def mock_agent(self):
        """创建 Mock Agent"""
        agent = MagicMock(spec=ScheduleAgent)
        agent.process_input = AsyncMock(return_value="这是测试响应")
        return agent

    @pytest.mark.asyncio
    async def test_run_single_command(self, mock_agent):
        """测试执行单条命令"""
        command = "明天的日程"
        response = await cli_module.run_single_command(mock_agent, command)

        assert response == "这是测试响应"
        mock_agent.process_input.assert_called_once_with(command)


class TestRunInteractiveLoop:
    """测试交互式循环"""

    @pytest.fixture
    def mock_agent(self):
        """创建 Mock Agent"""
        agent = MagicMock(spec=ScheduleAgent)
        agent.process_input = AsyncMock(return_value="这是测试响应")
        agent.clear_context = MagicMock()
        return agent

    @pytest.mark.asyncio
    async def test_exit_command_quit(self, mock_agent):
        """测试退出命令 quit"""
        with patch('builtins.input', side_effect=['quit']):
            await cli_module.run_interactive_loop(mock_agent)

        # 验证循环正常退出，没有调用 process_input
        mock_agent.process_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_command_exit(self, mock_agent):
        """测试退出命令 exit"""
        with patch('builtins.input', side_effect=['exit']):
            await cli_module.run_interactive_loop(mock_agent)

        mock_agent.process_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_command_q(self, mock_agent):
        """测试退出命令 q"""
        with patch('builtins.input', side_effect=['q']):
            await cli_module.run_interactive_loop(mock_agent)

        mock_agent.process_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_command(self, mock_agent):
        """测试清空对话命令"""
        with patch('builtins.input', side_effect=['clear', 'quit']):
            await cli_module.run_interactive_loop(mock_agent)

        mock_agent.clear_context.assert_called_once()

    @pytest.mark.asyncio
    async def test_help_command(self, mock_agent, capsys):
        """测试帮助命令"""
        with patch('builtins.input', side_effect=['help', 'quit']):
            await cli_module.run_interactive_loop(mock_agent)

        captured = capsys.readouterr()
        assert "帮助信息" in captured.out

    @pytest.mark.asyncio
    async def test_normal_input(self, mock_agent):
        """测试正常输入处理"""
        with patch('builtins.input', side_effect=['明天的日程', 'quit']):
            await cli_module.run_interactive_loop(mock_agent)

        mock_agent.process_input.assert_called_once_with('明天的日程')

    @pytest.mark.asyncio
    async def test_empty_input_skipped(self, mock_agent):
        """测试空输入被跳过"""
        with patch('builtins.input', side_effect=['', '   ', 'quit']):
            await cli_module.run_interactive_loop(mock_agent)

        mock_agent.process_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_keyboard_interrupt(self, mock_agent):
        """测试键盘中断"""
        with patch('builtins.input', side_effect=KeyboardInterrupt()):
            await cli_module.run_interactive_loop(mock_agent)

        # 应该正常退出，不抛出异常

    @pytest.mark.asyncio
    async def test_eof_error(self, mock_agent):
        """测试 EOF 错误"""
        with patch('builtins.input', side_effect=EOFError()):
            await cli_module.run_interactive_loop(mock_agent)

        # 应该正常退出，不抛出异常

    @pytest.mark.asyncio
    async def test_process_input_error(self, mock_agent):
        """测试处理输入时的错误"""
        mock_agent.process_input = AsyncMock(side_effect=Exception("测试错误"))

        with patch('builtins.input', side_effect=['测试输入', 'quit']):
            await cli_module.run_interactive_loop(mock_agent)

        # 应该捕获异常并继续运行


class TestMainAsync:
    """测试异步主函数"""

    @pytest.fixture
    def mock_config(self):
        """创建 Mock 配置"""
        return Config(
            llm=LLMConfig(
                api_key="test-api-key",
                model="test-model",
            ),
        )

    @pytest.mark.asyncio
    async def test_main_async_config_not_found(self):
        """测试配置文件不存在"""
        with patch('main.get_config', side_effect=FileNotFoundError("配置文件不存在")):
            with pytest.raises(SystemExit) as exc_info:
                await cli_module.main_async([])
            assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_main_async_interactive_mode(self, mock_config):
        """测试交互模式"""
        with patch('main.get_config', return_value=mock_config):
            with patch('main.ScheduleAgent') as MockAgent:
                mock_agent_instance = MagicMock()
                mock_agent_instance.__aenter__ = AsyncMock(return_value=mock_agent_instance)
                mock_agent_instance.__aexit__ = AsyncMock()
                MockAgent.return_value = mock_agent_instance

                with patch('main.run_interactive_loop', new_callable=AsyncMock) as mock_loop:
                    await cli_module.main_async([])
                    mock_loop.assert_called_once_with(mock_agent_instance)

    @pytest.mark.asyncio
    async def test_main_async_single_command(self, mock_config):
        """测试单条命令模式"""
        with patch('main.get_config', return_value=mock_config):
            with patch('main.ScheduleAgent') as MockAgent:
                mock_agent_instance = MagicMock()
                mock_agent_instance.__aenter__ = AsyncMock(return_value=mock_agent_instance)
                mock_agent_instance.__aexit__ = AsyncMock()
                MockAgent.return_value = mock_agent_instance

                with patch('main.run_single_command', new_callable=AsyncMock, return_value="响应") as mock_cmd:
                    with patch('builtins.print') as mock_print:
                        await cli_module.main_async(['main.py', '明天的日程'])
                        mock_cmd.assert_called_once()
                        mock_print.assert_called_with("响应")


class TestMain:
    """测试主入口"""

    def test_main_calls_asyncio_run(self):
        """测试 main 调用 asyncio.run"""
        with patch('asyncio.run') as mock_run:
            with patch('sys.argv', ['main.py']):
                cli_module.main()
                mock_run.assert_called_once()


class TestCLIIntegration:
    """CLI 集成测试"""

    @pytest.fixture
    def mock_config(self):
        """创建测试配置"""
        return Config(
            llm=LLMConfig(
                api_key="test-api-key",
                model="test-model",
            ),
        )

    @pytest.mark.asyncio
    async def test_full_interaction_flow(self, mock_config):
        """测试完整交互流程"""
        # 模拟用户输入序列
        inputs = [
            "hello",           # 正常输入
            "clear",           # 清空对话
            "help",            # 帮助
            "quit",            # 退出
        ]

        with patch('main.get_config', return_value=mock_config):
            with patch('main.ScheduleAgent') as MockAgent:
                # 设置 Mock Agent
                mock_agent = MagicMock()
                mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
                mock_agent.__aexit__ = AsyncMock()
                mock_agent.process_input = AsyncMock(return_value="响应内容")
                mock_agent.clear_context = MagicMock()
                MockAgent.return_value = mock_agent

                with patch('builtins.input', side_effect=inputs):
                    with patch('builtins.print'):
                        await cli_module.main_async([])

                # 验证 process_input 只被调用一次（hello）
                mock_agent.process_input.assert_called_once_with("hello")
                # 验证 clear_context 被调用
                mock_agent.clear_context.assert_called_once()
