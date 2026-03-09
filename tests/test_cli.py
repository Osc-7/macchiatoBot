"""
CLI 模块测试

测试命令行交互界面的功能。
"""

import asyncio
from types import SimpleNamespace
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from io import StringIO
import sys

from agent.config import (
    Config,
    LLMConfig,
    LoggingConfig,
    FileToolsConfig,
    CommandToolsConfig,
    MultimodalConfig,
    CanvasIntegrationConfig,
    MCPConfig,
    MCPServerConfig,
)
from agent.automation import AutomationCoreGateway
from agent.core import ScheduleAgent
from agent.core.adapters import ScheduleAgentAdapter
from agent.core.tools import BaseTool
from agent.cli.interactive import (
    print_welcome,
    print_help,
    run_interactive_loop,
)

# 导入 main 模块的函数（编排与入口）
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
            "update_event",
            "update_task",
            "delete_schedule_data",
            "get_free_slots",
            "plan_tasks",
            "sync_canvas",
        ]

        for expected in expected_tools:
            assert expected in tool_names, f"缺少工具: {expected}"

    def test_get_default_tools_includes_file_tools_when_enabled(self):
        """当 file_tools.enabled 时，应包含 read_file, write_file, modify_file"""
        config = Config(
            llm=LLMConfig(api_key="x", model="x"),
            file_tools=FileToolsConfig(enabled=True, allow_read=True),
        )
        tools = cli_module.get_default_tools(config=config)
        tool_names = [t.name for t in tools]
        assert "read_file" in tool_names
        assert "write_file" in tool_names
        assert "modify_file" in tool_names

    def test_get_default_tools_includes_run_command_when_enabled(self):
        """当 command_tools.enabled 时，应包含 run_command"""
        config = Config(
            llm=LLMConfig(api_key="x", model="x"),
            command_tools=CommandToolsConfig(enabled=True, allow_run=True),
        )
        tools = cli_module.get_default_tools(config=config)
        tool_names = [t.name for t in tools]
        assert "run_command" in tool_names

    def test_get_default_tools_includes_attach_media_when_enabled(self):
        """当 multimodal.enabled 时，应包含 attach_media"""
        config = Config(
            llm=LLMConfig(api_key="x", model="x"),
            multimodal=MultimodalConfig(enabled=True),
        )
        tools = cli_module.get_default_tools(config=config)
        tool_names = [t.name for t in tools]
        assert "attach_media" in tool_names

    def test_get_default_tools_includes_attach_image_to_reply_when_multimodal_enabled(self):
        """当 multimodal.enabled 时，应包含 attach_image_to_reply"""
        config = Config(
            llm=LLMConfig(api_key="x", model="x"),
            multimodal=MultimodalConfig(enabled=True),
        )
        tools = cli_module.get_default_tools(config=config)
        tool_names = [t.name for t in tools]
        assert "attach_image_to_reply" in tool_names

    def test_get_default_tools_includes_canvas_tool_when_enabled(self):
        """当 canvas.enabled 时，应包含 sync_canvas"""
        config = Config(
            llm=LLMConfig(api_key="x", model="x"),
            canvas=CanvasIntegrationConfig(enabled=True, api_key="dummy_canvas_key_12345"),
        )
        tools = cli_module.get_default_tools(config=config)
        tool_names = [t.name for t in tools]
        assert "sync_canvas" in tool_names


class TestPrintFunctions:
    """测试打印函数"""

    # def test_print_welcome(self, capsys):
    #     """测试打印欢迎信息"""
    #     print_welcome()
    #     captured = capsys.readouterr()

    #     assert "Schedule Agent" in captured.out
    #     assert "日程管理助手" in captured.out
    #     assert "quit" in captured.out
    #     assert "exit" in captured.out
    #     assert "clear" in captured.out
    #     assert "help" in captured.out
    @pytest.mark.skip(reason="暂时跳过打印欢迎信息测试")
    def test_print_help(self, capsys):
        """测试打印帮助信息"""
        print_help()
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

    @pytest.fixture(autouse=True)
    def disable_prompt_toolkit(self):
        """测试时禁用 prompt_toolkit，使用标准 input()"""
        import agent.cli.interactive as interactive_module
        with patch.object(interactive_module, '_HAS_PROMPT_TOOLKIT', False):
            yield

    @pytest.fixture
    def mock_agent(self):
        """创建 Mock Agent"""
        agent = MagicMock(spec=ScheduleAgent)
        agent.process_input = AsyncMock(return_value="这是测试响应")
        agent.clear_context = MagicMock()
        agent.get_token_usage = MagicMock(return_value={"call_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        return agent

    @pytest.mark.asyncio
    async def test_exit_command_quit(self, mock_agent):
        """测试退出命令 quit"""
        with patch('builtins.input', side_effect=['quit']):
            reason = await run_interactive_loop(mock_agent)

        assert reason == "quit"
        mock_agent.process_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_command_exit(self, mock_agent):
        """测试退出命令 exit"""
        with patch('builtins.input', side_effect=['exit']):
            reason = await run_interactive_loop(mock_agent)

        assert reason == "quit"
        mock_agent.process_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_command_q(self, mock_agent):
        """测试退出命令 q"""
        with patch('builtins.input', side_effect=['q']):
            reason = await run_interactive_loop(mock_agent)

        assert reason == "quit"
        mock_agent.process_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_command(self, mock_agent):
        """测试清空对话命令"""
        with patch('builtins.input', side_effect=['clear', 'quit']):
            await run_interactive_loop(mock_agent)

        mock_agent.clear_context.assert_called_once()

    @pytest.mark.asyncio
    async def test_help_command(self, mock_agent, capsys):
        """测试帮助命令"""
        with patch('builtins.input', side_effect=['help', 'quit']):
            await run_interactive_loop(mock_agent)

        captured = capsys.readouterr()
        assert "帮助信息" in captured.out

    @pytest.mark.asyncio
    async def test_normal_input(self, mock_agent):
        """测试正常输入处理"""
        with patch('builtins.input', side_effect=['明天的日程', 'quit']):
            await run_interactive_loop(mock_agent)

        mock_agent.process_input.assert_called_once()
        assert mock_agent.process_input.call_args.args[0] == '明天的日程'
        assert "on_stream_delta" in mock_agent.process_input.call_args.kwargs

    @pytest.mark.asyncio
    async def test_empty_input_skipped(self, mock_agent):
        """测试空输入被跳过"""
        with patch('builtins.input', side_effect=['', '   ', 'quit']):
            await run_interactive_loop(mock_agent)

        mock_agent.process_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_keyboard_interrupt(self, mock_agent):
        """测试键盘中断"""
        with patch('builtins.input', side_effect=KeyboardInterrupt()):
            reason = await run_interactive_loop(mock_agent)
        assert reason == "sigint"

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_cancelled_error_during_processing_returns_to_input(self, mock_agent, capsys):
        """测试处理阶段 CancelledError 仅中断当前轮并返回输入态"""
        mock_agent.process_input = AsyncMock(
            side_effect=[asyncio.CancelledError(), "这是第二次响应"]
        )

        with patch('builtins.input', side_effect=['测试输入', '再次输入', 'quit']):
            reason = await run_interactive_loop(mock_agent)

        assert reason == "quit"
        assert mock_agent.process_input.call_count == 2
        captured = capsys.readouterr()
        assert "已中断当前处理" in captured.out

    @pytest.mark.asyncio
    async def test_eof_error(self, mock_agent):
        """测试 EOF 错误"""
        with patch('builtins.input', side_effect=EOFError()):
            reason = await run_interactive_loop(mock_agent)
        assert reason == "eof"

    @pytest.mark.asyncio
    async def test_process_input_error(self, mock_agent):
        """测试处理输入时的错误"""
        mock_agent.process_input = AsyncMock(side_effect=Exception("测试错误"))

        with patch('builtins.input', side_effect=['测试输入', 'quit']):
            await run_interactive_loop(mock_agent)

        # 应该捕获异常并继续运行

    @pytest.mark.asyncio
    async def test_session_commands_new_list_switch(self):
        """测试 session 管理命令（new/list/switch）"""
        agent = MagicMock()
        agent.process_input = AsyncMock(return_value="不会调用")
        agent.clear_context = MagicMock()
        agent.get_token_usage = MagicMock(
            return_value={"call_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        agent.config = SimpleNamespace(memory=SimpleNamespace(idle_timeout_minutes=30))
        agent.mark_activity = MagicMock()
        agent.expire_session_if_needed = AsyncMock(return_value=False)
        agent.active_session_id = "cli:default"
        _sessions = ["cli:default"]

        def _list_sessions():
            return list(_sessions)

        async def _switch_session(session_id: str, create_if_missing: bool = True):
            created = False
            if session_id not in _sessions:
                if not create_if_missing:
                    raise KeyError(session_id)
                _sessions.append(session_id)
                created = True
            agent.active_session_id = session_id
            return created

        agent.list_sessions = _list_sessions
        agent.switch_session = AsyncMock(side_effect=_switch_session)

        with patch("builtins.input", side_effect=["session", "session new cli:work", "session list", "session switch cli:default", "quit"]):
            reason = await run_interactive_loop(agent)

        assert reason == "quit"
        agent.process_input.assert_not_called()
        assert "cli:work" in _sessions
        assert agent.active_session_id == "cli:default"

    @pytest.mark.asyncio
    async def test_session_switch_missing_session_shows_hint(self, capsys):
        """测试切换到不存在会话时给出提示"""
        agent = MagicMock()
        agent.process_input = AsyncMock(return_value="不会调用")
        agent.clear_context = MagicMock()
        agent.get_token_usage = MagicMock(
            return_value={"call_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        agent.config = SimpleNamespace(memory=SimpleNamespace(idle_timeout_minutes=30))
        agent.mark_activity = MagicMock()
        agent.expire_session_if_needed = AsyncMock(return_value=False)
        agent.active_session_id = "cli:default"
        agent.list_sessions = MagicMock(return_value=["cli:default"])
        agent.switch_session = AsyncMock()

        with patch("builtins.input", side_effect=["session switch cli:missing", "quit"]):
            reason = await run_interactive_loop(agent)

        assert reason == "quit"
        agent.switch_session.assert_not_awaited()
        captured = capsys.readouterr()
        assert "会话不存在" in captured.out

    @pytest.mark.asyncio
    async def test_session_whoami_shows_owner_source_session(self, capsys):
        """测试 session whoami 输出 user/source/session"""
        agent = MagicMock()
        agent.process_input = AsyncMock(return_value="不会调用")
        agent.clear_context = MagicMock()
        agent.get_token_usage = MagicMock(
            return_value={"call_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        agent.config = SimpleNamespace(memory=SimpleNamespace(idle_timeout_minutes=30))
        agent.mark_activity = MagicMock()
        agent.expire_session_if_needed = AsyncMock(return_value=False)
        agent.active_session_id = "cli:default"
        agent.owner_id = "root"
        agent.source = "cli"
        agent.list_sessions = MagicMock(return_value=["cli:default"])
        agent.switch_session = AsyncMock()

        with patch("builtins.input", side_effect=["session whoami", "quit"]):
            reason = await run_interactive_loop(agent)

        assert reason == "quit"
        captured = capsys.readouterr()
        assert "user=root" in captured.out
        assert "source=cli" in captured.out
        assert "session=cli:default" in captured.out


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
            logging=LoggingConfig(enable_session_log=False),
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
                mock_agent_instance.finalize_session = AsyncMock()
                MockAgent.return_value = mock_agent_instance

                with patch('main.run_interactive_loop', new_callable=AsyncMock, return_value="quit") as mock_loop:
                    await cli_module.main_async(["main.py", "--local"])
                    mock_loop.assert_called_once()
                    wrapped = mock_loop.call_args.args[0]
                    assert isinstance(wrapped, AutomationCoreGateway)
                    assert isinstance(wrapped.raw_core_session, ScheduleAgentAdapter)
                    assert wrapped.raw_core_session.raw_agent is mock_agent_instance
                mock_agent_instance.finalize_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_main_async_skip_finalize_on_sigint_exit(self, mock_config):
        """测试交互循环因 Ctrl+C 退出时不执行 finalize_session"""
        with patch('main.get_config', return_value=mock_config):
            with patch('main.ScheduleAgent') as MockAgent:
                mock_agent_instance = MagicMock()
                mock_agent_instance.__aenter__ = AsyncMock(return_value=mock_agent_instance)
                mock_agent_instance.__aexit__ = AsyncMock()
                mock_agent_instance.finalize_session = AsyncMock()
                MockAgent.return_value = mock_agent_instance

                with patch('main.run_interactive_loop', new_callable=AsyncMock, return_value="sigint"):
                    await cli_module.main_async(["main.py", "--local"])

                mock_agent_instance.finalize_session.assert_not_called()

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
                        await cli_module.main_async(["main.py", "--local", "明天的日程"])
                        mock_cmd.assert_called_once()
                        mock_print.assert_called_with("响应")

    @pytest.mark.asyncio
    async def test_main_async_with_mcp_enabled(self):
        """测试启用 MCP 时 Agent 内部会连接并关闭 MCP 管理器"""
        mock_config = Config(
            llm=LLMConfig(api_key="test-api-key", model="test-model"),
            logging=LoggingConfig(enable_session_log=False),
            mcp=MCPConfig(
                enabled=True,
                servers=[
                    MCPServerConfig(
                        name="demo",
                        command="python",
                        args=["-m", "demo_server"],
                    )
                ],
            ),
        )
        with patch('main.get_config', return_value=mock_config):
            # MCPClientManager 现在在 ScheduleAgent 内部处理
            with patch('agent.core.agent.agent.MCPClientManager') as MockMCPManager:
                mock_mcp = MagicMock()
                mock_mcp.connect = AsyncMock()
                mock_mcp.get_proxy_tools = MagicMock(return_value=[])
                mock_mcp.close = AsyncMock()
                MockMCPManager.return_value = mock_mcp

                with patch('main.run_interactive_loop', new_callable=AsyncMock):
                    await cli_module.main_async(["main.py", "--local"])

                # MCP 在 __aenter__ 中初始化
                mock_mcp.connect.assert_called_once()
                mock_mcp.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_async_auto_add_local_mcp_server(self):
        """测试启用 MCP 且未配置 server 时自动注入本地 mcp_server.py"""
        mock_config = Config(
            llm=LLMConfig(api_key="test-api-key", model="test-model"),
            logging=LoggingConfig(enable_session_log=False),
            mcp=MCPConfig(enabled=True, servers=[]),
        )
        with patch('main.get_config', return_value=mock_config):
            with patch('agent.core.agent.agent.MCPClientManager') as MockMCPManager:
                mock_mcp = MagicMock()
                mock_mcp.connect = AsyncMock()
                mock_mcp.get_proxy_tools = MagicMock(return_value=[])
                mock_mcp.close = AsyncMock()
                MockMCPManager.return_value = mock_mcp

                with patch('main.run_interactive_loop', new_callable=AsyncMock):
                    await cli_module.main_async(["main.py", "--local"])

                assert MockMCPManager.call_count == 1
                runtime_mcp = MockMCPManager.call_args[0][0]
                assert runtime_mcp.enabled is True
                # 自动注入了 schedule_tools
                assert len(runtime_mcp.servers) == 1
                assert runtime_mcp.servers[0].name == "schedule_tools"
                assert any("mcp_server.py" in arg for arg in runtime_mcp.servers[0].args)


class TestMain:
    """测试主入口"""

    def test_main_calls_main_async(self):
        """测试 main 会执行 main_async"""
        with patch('main.main_async', new_callable=AsyncMock) as mock_main_async:
            with patch('sys.argv', ['main.py']):
                cli_module.main()
                mock_main_async.assert_called_once()

    def test_main_handles_keyboard_interrupt_from_main_async(self):
        """测试 main 在 main_async 中断时不向外传播"""
        with patch('main.main_async', new_callable=AsyncMock, side_effect=KeyboardInterrupt()):
            with patch('sys.argv', ['main.py']):
                cli_module.main()


class TestCLIIntegration:
    """CLI 集成测试"""

    @pytest.fixture(autouse=True)
    def disable_prompt_toolkit(self):
        """测试时禁用 prompt_toolkit"""
        import agent.cli.interactive as interactive_module
        with patch.object(interactive_module, '_HAS_PROMPT_TOOLKIT', False):
            yield

    @pytest.fixture
    def mock_config(self):
        """创建测试配置"""
        return Config(
            llm=LLMConfig(
                api_key="test-api-key",
                model="test-model",
            ),
            logging=LoggingConfig(enable_session_log=False),
        )

    @pytest.mark.asyncio
    async def test_full_interaction_flow(self, mock_config):
        """测试完整交互流程"""
        inputs = [
            "hello",           # 正常输入
            "clear",           # 清空对话
            "help",            # 帮助
            "quit",            # 退出
        ]

        from agent.core.interfaces import AgentRunResult
        from agent.automation.core_gateway import AutomationCoreGateway

        with patch('main.get_config', return_value=mock_config):
            with patch('main.ScheduleAgent') as MockAgent:
                mock_agent = MagicMock()
                mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
                mock_agent.__aexit__ = AsyncMock()
                mock_agent.process_input = AsyncMock(return_value="响应内容")
                mock_agent.clear_context = MagicMock()
                mock_agent.get_token_usage = MagicMock(return_value={"call_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
                MockAgent.return_value = mock_agent

                # Gateway 是 CLI 实际调用的对象；mock 其 run_turn 验证整合流程
                mock_gateway_run_turn = AsyncMock(
                    return_value=AgentRunResult(output_text="响应内容")
                )
                with patch.object(AutomationCoreGateway, 'run_turn', mock_gateway_run_turn):
                    with patch('builtins.input', side_effect=inputs):
                        with patch('builtins.print'):
                            await cli_module.main_async(["main.py", "--local"])

                # CLI 通过 gateway.run_turn 调用 Agent
                mock_gateway_run_turn.assert_called_once()
                call_args = mock_gateway_run_turn.call_args
                assert call_args.args[0].text == "hello"
                mock_agent.clear_context.assert_called_once()
