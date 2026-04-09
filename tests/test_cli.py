"""
CLI 模块测试

测试命令行交互界面的功能。
"""

import asyncio
from types import SimpleNamespace
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_core.config import (
    Config,
    LLMConfig,
    FileToolsConfig,
    CommandToolsConfig,
    MultimodalConfig,
    CanvasIntegrationConfig,
)
from agent_core.tools import BaseTool
from system.tools import get_default_tools
from agent_core.interfaces import AgentRunResult
from frontend.cli.interactive import (
    print_help,
    run_interactive_loop,
)

import main as cli_module


class TestGetDefaultTools:
    """测试获取默认工具功能"""

    def test_get_default_tools_returns_list(self):
        """测试返回工具列表"""
        tools = get_default_tools()
        assert isinstance(tools, list)
        assert len(tools) > 0

    def test_get_default_tools_all_are_base_tool(self):
        """测试所有工具都是 BaseTool 实例"""
        tools = get_default_tools()
        for tool in tools:
            assert isinstance(tool, BaseTool)

    def test_get_default_tools_contains_expected_tools(self):
        """测试包含预期的工具"""
        tools = get_default_tools()
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
        tools = get_default_tools(config=config)
        tool_names = [t.name for t in tools]
        assert "read_file" in tool_names
        assert "write_file" in tool_names
        assert "modify_file" in tool_names

    def test_get_default_tools_no_bash_in_registry(self):
        """BashTool 由 AgentCore 自注册，不在 get_default_tools 返回中"""
        config = Config(
            llm=LLMConfig(api_key="x", model="x"),
            command_tools=CommandToolsConfig(enabled=True, allow_run=True),
        )
        tools = get_default_tools(config=config)
        tool_names = [t.name for t in tools]
        assert "bash" not in tool_names
        assert "run_command" not in tool_names

    def test_get_default_tools_includes_attach_media_when_enabled(self):
        """当 multimodal.enabled 时，应包含 attach_media"""
        config = Config(
            llm=LLMConfig(api_key="x", model="x"),
            multimodal=MultimodalConfig(enabled=True),
        )
        tools = get_default_tools(config=config)
        tool_names = [t.name for t in tools]
        assert "attach_media" in tool_names

    def test_get_default_tools_includes_attach_image_to_reply_when_multimodal_enabled(
        self,
    ):
        """当 multimodal.enabled 时，应包含 attach_image_to_reply"""
        config = Config(
            llm=LLMConfig(api_key="x", model="x"),
            multimodal=MultimodalConfig(enabled=True),
        )
        tools = get_default_tools(config=config)
        tool_names = [t.name for t in tools]
        assert "attach_image_to_reply" in tool_names

    def test_get_default_tools_includes_canvas_tool_when_enabled(self):
        """当 canvas.enabled 时，应包含 sync_canvas"""
        config = Config(
            llm=LLMConfig(api_key="x", model="x"),
            canvas=CanvasIntegrationConfig(
                enabled=True, api_key="dummy_canvas_key_12345"
            ),
        )
        tools = get_default_tools(config=config)
        tool_names = [t.name for t in tools]
        assert "sync_canvas" in tool_names


class TestPrintFunctions:
    """测试打印函数"""

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

    @pytest.mark.asyncio
    async def test_run_single_command_with_run_turn(self):
        """测试通过 run_turn 执行单条命令"""
        agent = MagicMock()
        agent.run_turn = AsyncMock(
            return_value=AgentRunResult(output_text="run_turn 响应")
        )
        response = await cli_module.run_single_command(agent, "测试")
        assert response == "run_turn 响应"


class TestRunInteractiveLoop:
    """测试交互式循环"""

    @pytest.fixture(autouse=True)
    def disable_prompt_toolkit(self):
        """测试时禁用 prompt_toolkit，使用标准 input()"""
        import frontend.cli.interactive as interactive_module

        with patch.object(interactive_module, "_HAS_PROMPT_TOOLKIT", False):
            yield

    @pytest.fixture
    def mock_agent(self):
        """创建 Mock Agent，run_turn 返回 AgentRunResult"""
        agent = MagicMock()
        agent.run_turn = AsyncMock(
            return_value=AgentRunResult(output_text="这是测试响应")
        )
        agent.clear_context = MagicMock()
        agent.get_token_usage = MagicMock(
            return_value={
                "call_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        )
        return agent

    @pytest.mark.asyncio
    async def test_exit_command_quit(self, mock_agent):
        """测试退出命令 quit"""
        with patch("builtins.input", side_effect=["quit"]):
            reason = await run_interactive_loop(mock_agent)

        assert reason == "quit"
        mock_agent.process_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_command_exit(self, mock_agent):
        """测试退出命令 exit"""
        with patch("builtins.input", side_effect=["exit"]):
            reason = await run_interactive_loop(mock_agent)

        assert reason == "quit"
        mock_agent.process_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_command_q(self, mock_agent):
        """测试退出命令 q"""
        with patch("builtins.input", side_effect=["q"]):
            reason = await run_interactive_loop(mock_agent)

        assert reason == "quit"
        mock_agent.process_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_command(self, mock_agent):
        """测试清空对话命令"""
        with patch("builtins.input", side_effect=["clear", "quit"]):
            await run_interactive_loop(mock_agent)

        mock_agent.clear_context.assert_called_once()

    @pytest.mark.asyncio
    async def test_help_command(self, mock_agent, capsys):
        """测试帮助命令"""
        with patch("builtins.input", side_effect=["help", "quit"]):
            await run_interactive_loop(mock_agent)

        captured = capsys.readouterr()
        assert "帮助信息" in captured.out

    @pytest.mark.asyncio
    async def test_normal_input(self, mock_agent):
        """测试正常输入处理"""
        with patch("builtins.input", side_effect=["明天的日程", "quit"]):
            await run_interactive_loop(mock_agent)

        mock_agent.run_turn.assert_called_once()
        run_input = mock_agent.run_turn.call_args.args[0]
        assert run_input.text == "明天的日程"

    @pytest.mark.asyncio
    async def test_empty_input_skipped(self, mock_agent):
        """测试空输入被跳过"""
        with patch("builtins.input", side_effect=["", "   ", "quit"]):
            await run_interactive_loop(mock_agent)

        mock_agent.process_input.assert_not_called()

    @pytest.mark.asyncio
    async def test_keyboard_interrupt(self, mock_agent):
        """测试键盘中断"""
        with patch("builtins.input", side_effect=KeyboardInterrupt()):
            reason = await run_interactive_loop(mock_agent)
        assert reason == "sigint"

    @pytest.mark.asyncio
    async def test_cancelled_error_during_processing_returns_to_input(
        self, mock_agent, capsys
    ):
        """测试处理阶段 CancelledError 仅中断当前轮并返回输入态"""
        mock_agent.run_turn = AsyncMock(
            side_effect=[
                asyncio.CancelledError(),
                AgentRunResult(output_text="这是第二次响应"),
            ]
        )

        with patch("builtins.input", side_effect=["测试输入", "再次输入", "quit"]):
            reason = await run_interactive_loop(mock_agent)

        assert reason == "quit"
        assert mock_agent.run_turn.call_count == 2
        captured = capsys.readouterr()
        assert "已中断当前处理" in captured.out

    @pytest.mark.asyncio
    async def test_eof_error(self, mock_agent):
        """测试 EOF 错误"""
        with patch("builtins.input", side_effect=EOFError()):
            reason = await run_interactive_loop(mock_agent)
        assert reason == "eof"

    @pytest.mark.asyncio
    async def test_process_input_error(self, mock_agent):
        """测试处理输入时的错误"""
        mock_agent.run_turn = AsyncMock(side_effect=Exception("测试错误"))

        with patch("builtins.input", side_effect=["测试输入", "quit"]):
            await run_interactive_loop(mock_agent)

    @pytest.mark.asyncio
    async def test_session_commands_new_list_switch(self):
        """测试 session 管理命令（new/list/switch）"""
        agent = MagicMock()
        agent.process_input = AsyncMock(return_value="不会调用")
        agent.clear_context = MagicMock()
        agent.get_token_usage = MagicMock(
            return_value={
                "call_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        )
        agent.config = SimpleNamespace(memory=SimpleNamespace(idle_timeout_minutes=30))
        agent.mark_activity = MagicMock()
        agent.expire_session_if_needed = AsyncMock(return_value=False)
        agent.active_session_id = "cli:root"
        _sessions = ["cli:root"]

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

        with patch(
            "builtins.input",
            side_effect=[
                "session",
                "session new cli:work",
                "session list",
                "session switch cli:root",
                "quit",
            ],
        ):
            reason = await run_interactive_loop(agent)

        assert reason == "quit"
        agent.process_input.assert_not_called()
        assert "cli:work" in _sessions
        assert agent.active_session_id == "cli:root"

    @pytest.mark.asyncio
    async def test_session_switch_missing_session_shows_hint(self, capsys):
        """测试切换到不存在会话时给出提示"""
        agent = MagicMock()
        agent.process_input = AsyncMock(return_value="不会调用")
        agent.clear_context = MagicMock()
        agent.get_token_usage = MagicMock(
            return_value={
                "call_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        )
        agent.config = SimpleNamespace(memory=SimpleNamespace(idle_timeout_minutes=30))
        agent.mark_activity = MagicMock()
        agent.expire_session_if_needed = AsyncMock(return_value=False)
        agent.active_session_id = "cli:root"
        agent.list_sessions = MagicMock(return_value=["cli:root"])
        agent.switch_session = AsyncMock()

        with patch(
            "builtins.input", side_effect=["session switch cli:missing", "quit"]
        ):
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
            return_value={
                "call_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        )
        agent.config = SimpleNamespace(memory=SimpleNamespace(idle_timeout_minutes=30))
        agent.mark_activity = MagicMock()
        agent.expire_session_if_needed = AsyncMock(return_value=False)
        agent.active_session_id = "cli:root"
        agent.owner_id = "root"
        agent.source = "cli"
        agent.list_sessions = MagicMock(return_value=["cli:root"])
        agent.switch_session = AsyncMock()

        with patch("builtins.input", side_effect=["session whoami", "quit"]):
            reason = await run_interactive_loop(agent)

        assert reason == "quit"
        captured = capsys.readouterr()
        assert "user=root" in captured.out
        assert "source=cli" in captured.out
        assert "session=cli:root" in captured.out


class TestMainAsync:
    """测试异步主函数（IPC 模式）"""

    @pytest.mark.asyncio
    async def test_main_async_config_not_found(self):
        """测试配置文件不存在"""
        with patch("main.get_config", side_effect=FileNotFoundError("配置文件不存在")):
            with pytest.raises(SystemExit) as exc_info:
                await cli_module.main_async([])
            assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_main_async_daemon_not_running(self):
        """测试 daemon 未运行时报错退出"""
        mock_config = Config(llm=LLMConfig(api_key="x", model="x"))
        with patch("main.get_config", return_value=mock_config):
            with patch("main.AutomationIPCClient") as MockIPC:
                mock_ipc = MagicMock()
                mock_ipc.ping = AsyncMock(return_value=False)
                MockIPC.return_value = mock_ipc

                with pytest.raises(SystemExit) as exc_info:
                    await cli_module.main_async(["main.py"])
                assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_main_async_interactive_mode(self):
        """测试交互模式（IPC）"""
        mock_config = Config(llm=LLMConfig(api_key="x", model="x"))
        with patch("main.get_config", return_value=mock_config):
            with patch("main.AutomationIPCClient") as MockIPC:
                mock_ipc = MagicMock()
                mock_ipc.ping = AsyncMock(return_value=True)
                mock_ipc.connect = AsyncMock()
                mock_ipc.close = AsyncMock()
                MockIPC.return_value = mock_ipc

                with patch(
                    "main.run_interactive_loop",
                    new_callable=AsyncMock,
                    return_value="quit",
                ) as mock_loop:
                    await cli_module.main_async(["main.py"])
                    mock_loop.assert_called_once_with(mock_ipc)

                mock_ipc.connect.assert_called_once()
                mock_ipc.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_async_single_command(self):
        """测试单条命令模式（IPC）"""
        mock_config = Config(llm=LLMConfig(api_key="x", model="x"))
        with patch("main.get_config", return_value=mock_config):
            with patch("main.AutomationIPCClient") as MockIPC:
                mock_ipc = MagicMock()
                mock_ipc.ping = AsyncMock(return_value=True)
                mock_ipc.connect = AsyncMock()
                mock_ipc.close = AsyncMock()
                MockIPC.return_value = mock_ipc

                with patch(
                    "main.run_single_command",
                    new_callable=AsyncMock,
                    return_value="响应",
                ) as mock_cmd:
                    with patch("builtins.print") as mock_print:
                        await cli_module.main_async(["main.py", "明天的日程"])
                        mock_cmd.assert_called_once()
                        mock_print.assert_called_with("响应")

    @pytest.mark.asyncio
    async def test_main_async_close_on_exception(self):
        """测试异常时也能正确关闭 IPC 连接"""
        mock_config = Config(llm=LLMConfig(api_key="x", model="x"))
        with patch("main.get_config", return_value=mock_config):
            with patch("main.AutomationIPCClient") as MockIPC:
                mock_ipc = MagicMock()
                mock_ipc.ping = AsyncMock(return_value=True)
                mock_ipc.connect = AsyncMock()
                mock_ipc.close = AsyncMock()
                MockIPC.return_value = mock_ipc

                with patch(
                    "main.run_interactive_loop",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("测试异常"),
                ):
                    with pytest.raises(RuntimeError, match="测试异常"):
                        await cli_module.main_async(["main.py"])

                mock_ipc.close.assert_called_once()


class TestMain:
    """测试主入口"""

    def test_main_calls_main_async(self):
        """测试 main 会执行 main_async"""
        with patch("main.main_async", new_callable=AsyncMock) as mock_main_async:
            with patch("sys.argv", ["main.py"]):
                cli_module.main()
                mock_main_async.assert_called_once()

    def test_main_handles_keyboard_interrupt_from_main_async(self):
        """测试 main 在 main_async 中断时不向外传播"""
        with patch(
            "main.main_async", new_callable=AsyncMock, side_effect=KeyboardInterrupt()
        ):
            with patch("sys.argv", ["main.py"]):
                cli_module.main()
