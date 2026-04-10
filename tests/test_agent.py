"""
Agent 测试用例

测试 AgentCore 的核心功能。
"""

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, patch

import pytest

from agent_core.config import AgentConfig, Config, LLMConfig
from agent_core.agent import AgentCore
from agent_core.context import ConversationContext
from agent_core.llm import LLMResponse, ToolCall
from agent_core.tools import BaseTool, ToolDefinition, ToolParameter, ToolResult


# ============== 测试工具 ==============


class MockTool(BaseTool):
    """测试用的 Mock 工具"""

    def __init__(
        self,
        name: str = "mock_tool",
        execute_result: Optional[ToolResult] = None,
    ):
        self._name = name
        self._execute_result = execute_result or ToolResult(
            success=True,
            message="Mock tool executed",
            data={"result": "ok"},
        )
        self.execute_called = False
        self.execute_kwargs: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self._name,
            description=f"Mock tool for testing: {self._name}",
            parameters=[
                ToolParameter(
                    name="input",
                    type="string",
                    description="Input parameter",
                    required=True,
                )
            ],
        )

    async def execute(self, **kwargs) -> ToolResult:
        self.execute_called = True
        self.execute_kwargs = kwargs
        return self._execute_result


# ============== Fixtures ==============


@pytest.fixture
def mock_config():
    """创建 Mock 配置。使用 sub 模式使传入工具直接可见，便于单测。"""
    return Config(
        llm=LLMConfig(
            api_key="test-api-key",
            model="test-model",
            temperature=0.7,
            max_tokens=4096,
        ),
        agent=AgentConfig(
            max_iterations=5,
            enable_debug=False,
            tool_mode="sub",
            source_overrides={},
        ),
    )


@pytest.fixture
def mock_tools():
    """创建 Mock 工具列表"""
    return [
        MockTool(name="tool_a"),
        MockTool(name="tool_b"),
    ]


@pytest.fixture
def agent(mock_config, mock_tools):
    """创建 Agent 实例"""
    return AgentCore(
        config=mock_config,
        tools=mock_tools,
        max_iterations=5,
    )


# ============== 初始化测试 ==============


class TestAgentCoreInit:
    """测试 Agent 初始化"""

    def test_init_with_config(self, mock_config, mock_tools):
        """测试使用配置初始化"""
        agent = AgentCore(
            config=mock_config,
            tools=mock_tools,
            max_iterations=10,
            timezone="America/New_York",
        )

        assert agent._config is mock_config
        assert agent._max_iterations == 10
        assert agent._timezone == "America/New_York"
        # 2 custom + 3 chat + search_tools + call_tool（meta 全 Core 注册）
        assert len(agent.tool_registry) == 7

    def test_init_without_tools(self, mock_config):
        """测试不传入工具时初始化"""
        agent = AgentCore(config=mock_config)

        # 3 chat + search_tools + call_tool
        assert len(agent.tool_registry) == 5
        assert isinstance(agent.context, ConversationContext)

    def test_tool_registry_property(self, agent):
        """测试工具注册表属性"""
        registry = agent.tool_registry
        assert registry.has("tool_a")
        assert registry.has("tool_b")

    def test_context_property(self, agent):
        """测试上下文属性"""
        context = agent.context
        assert isinstance(context, ConversationContext)


# ============== 工具注册测试 ==============


class TestToolRegistration:
    """测试工具注册"""

    def test_register_tool(self, agent):
        """测试注册新工具"""
        new_tool = MockTool(name="tool_c")
        agent.register_tool(new_tool)

        assert agent.tool_registry.has("tool_c")
        # 2 original + 1 new + 3 chat + meta
        assert len(agent.tool_registry) == 8

    def test_unregister_tool(self, agent):
        """测试注销工具"""
        result = agent.unregister_tool("tool_a")

        assert result is True
        assert not agent.tool_registry.has("tool_a")
        # 2 original - 1 removed + 3 chat + meta
        assert len(agent.tool_registry) == 6

    def test_unregister_nonexistent_tool(self, agent):
        """测试注销不存在的工具"""
        result = agent.unregister_tool("nonexistent")

        assert result is False


# ============== 上下文管理测试 ==============


class TestContextManagement:
    """测试上下文管理"""

    def test_clear_context(self, agent):
        """测试清空上下文"""
        agent.context.add_user_message("Hello")
        agent.context.add_assistant_message("Hi there!")

        assert len(agent.context) == 2

        agent.clear_context()

        assert len(agent.context) == 0


# ============== 系统提示构建测试 ==============


class TestBuildSystemPrompt:
    """测试系统提示构建"""

    def test_build_system_prompt_contains_time_context(self, agent):
        """测试系统提示包含时间上下文"""
        prompt = agent._build_system_prompt()

        assert "当前时间上下文" in prompt
        assert "当前时间:" in prompt
        assert "日期:" in prompt
        assert "时区:" in prompt

    def test_build_system_prompt_has_no_working_memory_section(self, agent):
        """工作记忆仅为滑动窗口，system 不再含「# 工作记忆摘要」。"""
        agent._memory_enabled = True
        agent._working_memory.running_summary = "不应出现在 system"
        prompt = agent._build_system_prompt()
        assert "# 工作记忆摘要" not in prompt

    @pytest.mark.skip(reason="不再需要这些信息")
    def test_build_system_prompt_contains_agent_info(self, agent):
        """测试系统提示包含 Agent 信息"""
        prompt = agent._build_system_prompt()

        assert ("智能日程管理助手" in prompt) or ("人工智能助手" in prompt)
        assert "创建和管理日程事件" in prompt
        assert "创建和管理待办任务" in prompt


# ============== 工具调用处理测试 ==============


class TestToolCallExecution:
    """测试工具调用执行"""

    @pytest.mark.asyncio
    async def test_execute_tool_call_with_dict_args(self, agent):
        """测试使用字典参数执行工具调用"""
        tool_call = ToolCall(
            id="call_123",
            name="tool_a",
            arguments={"input": "test_value"},
        )

        result = await agent._execute_tool_call(tool_call)

        assert result.success is True
        assert result.message == "Mock tool executed"

    @pytest.mark.asyncio
    async def test_execute_tool_call_with_json_args(self, agent):
        """测试使用 JSON 字符串参数执行工具调用"""
        tool_call = ToolCall(
            id="call_123",
            name="tool_a",
            arguments='{"input": "test_value"}',
        )

        result = await agent._execute_tool_call(tool_call)

        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_tool_call_with_invalid_json(self, agent):
        """测试无效 JSON 参数"""
        tool_call = ToolCall(
            id="call_123",
            name="tool_a",
            arguments="not valid json",
        )

        result = await agent._execute_tool_call(tool_call)

        assert result.success is False
        assert result.error == "INVALID_ARGUMENTS"

    @pytest.mark.asyncio
    async def test_execute_nonexistent_tool(self, agent):
        """测试执行不存在的工具"""
        tool_call = ToolCall(
            id="call_123",
            name="nonexistent_tool",
            arguments={},
        )

        result = await agent._execute_tool_call(tool_call)

        assert result.success is False
        assert result.error == "TOOL_NOT_FOUND"


# ============== 消息处理测试 ==============


class TestAddAssistantMessage:
    """测试添加助手消息"""

    def test_add_assistant_message_with_tool_calls(self, agent):
        """测试添加包含工具调用的助手消息"""
        response = LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="tool_a",
                    arguments={"input": "value"},
                ),
            ],
        )

        agent._add_assistant_message_with_tool_calls(response)

        messages = agent.context.get_messages()
        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert "tool_calls" in messages[0]
        assert len(messages[0]["tool_calls"]) == 1

    def test_add_assistant_message_with_json_args(self, agent):
        """测试工具调用参数为 JSON 字符串"""
        response = LLMResponse(
            content="Thinking...",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="tool_a",
                    arguments='{"input": "value"}',
                ),
            ],
        )

        agent._add_assistant_message_with_tool_calls(response)

        messages = agent.context.get_messages()
        tool_call = messages[0]["tool_calls"][0]
        # 参数应该是字符串格式
        assert isinstance(tool_call["function"]["arguments"], str)


# ============== 主循环测试 ==============


class TestProcessInput:
    """测试主输入处理循环"""

    @pytest.mark.asyncio
    async def test_process_input_simple_response(self, agent):
        """测试简单响应（无工具调用）"""
        mock_response = LLMResponse(
            content="你好！有什么我可以帮助你的吗？",
            tool_calls=[],
        )

        with patch.object(
            agent._llm_client,
            "chat_with_tools",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await agent.process_input("你好")

        assert result == "你好！有什么我可以帮助你的吗？"
        assert len(agent.context) == 2  # user + assistant

    @pytest.mark.asyncio
    async def test_process_input_with_tool_call(self, agent):
        """测试带工具调用的处理"""
        # 第一次响应：包含工具调用
        tool_call_response = LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="tool_a",
                    arguments={"input": "test"},
                ),
            ],
        )

        # 第二次响应：最终响应
        final_response = LLMResponse(
            content="工具执行成功！",
            tool_calls=[],
        )

        with patch.object(
            agent._llm_client,
            "chat_with_tools",
            new_callable=AsyncMock,
            side_effect=[tool_call_response, final_response],
        ):
            result = await agent.process_input("执行工具")

        assert result == "工具执行成功！"

    @pytest.mark.asyncio
    async def test_process_input_injects_media_next_call_when_flagged(
        self, mock_config
    ):
        """当工具结果声明 embed_in_next_call 时，下一轮请求应携带多模态内容。"""
        media_tool = MockTool(
            name="tool_a",
            execute_result=ToolResult(
                success=True,
                message="媒体已就绪",
                data={"path": "user_file/page_1.png"},
                metadata={"embed_in_next_call": True},
            ),
        )
        agent = AgentCore(config=mock_config, tools=[media_tool], max_iterations=5)

        response1 = LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="call_1", name="tool_a", arguments={"input": "x"})],
        )
        response2 = LLMResponse(content="已根据图片继续分析。", tool_calls=[])

        with (
            patch(
                "agent_core.agent.agent.resolve_media_to_content_item",
                return_value=(
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAA"},
                    },
                    None,
                ),
            ),
            patch.object(
                agent._llm_client,
                "chat_with_tools",
                new_callable=AsyncMock,
                side_effect=[response1, response2],
            ) as mocked_chat,
        ):
            result = await agent.process_input("请继续")

        assert result == "已根据图片继续分析。"
        assert mocked_chat.await_count == 2
        second_call_messages = mocked_chat.await_args_list[1].kwargs["messages"]
        injected = second_call_messages[-1]
        assert injected["role"] == "user"
        assert isinstance(injected["content"], list)
        assert injected["content"][1]["type"] == "image_url"

    @pytest.mark.asyncio
    async def test_process_input_does_not_inject_media_without_flag(self, mock_config):
        """工具结果未声明 embed_in_next_call 时，不应注入多模态消息。"""
        plain_tool = MockTool(
            name="tool_a",
            execute_result=ToolResult(
                success=True,
                message="ok",
                data={"path": "user_file/page_1.png"},
                metadata={},
            ),
        )
        agent = AgentCore(config=mock_config, tools=[plain_tool], max_iterations=5)

        response1 = LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="call_1", name="tool_a", arguments={"input": "x"})],
        )
        response2 = LLMResponse(content="done", tool_calls=[])

        with patch.object(
            agent._llm_client,
            "chat_with_tools",
            new_callable=AsyncMock,
            side_effect=[response1, response2],
        ) as mocked_chat:
            result = await agent.process_input("请继续")

        assert result == "done"
        second_call_messages = mocked_chat.await_args_list[1].kwargs["messages"]
        assert not (
            second_call_messages
            and second_call_messages[-1].get("role") == "user"
            and isinstance(second_call_messages[-1].get("content"), list)
        )

    @pytest.mark.asyncio
    async def test_process_input_multiple_tool_calls(self, agent):
        """测试多次工具调用"""
        # 第一次：工具调用 A
        response1 = LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="tool_a",
                    arguments={"input": "a"},
                ),
            ],
        )

        # 第二次：工具调用 B
        response2 = LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call_2",
                    name="tool_b",
                    arguments={"input": "b"},
                ),
            ],
        )

        # 第三次：最终响应
        response3 = LLMResponse(
            content="所有工具执行完成！",
            tool_calls=[],
        )

        with patch.object(
            agent._llm_client,
            "chat_with_tools",
            new_callable=AsyncMock,
            side_effect=[response1, response2, response3],
        ):
            result = await agent.process_input("执行多个工具")

        assert result == "所有工具执行完成！"

    @pytest.mark.asyncio
    async def test_process_input_max_iterations(self, mock_config):
        """测试超过最大迭代次数"""
        # 创建一个会一直返回工具调用的响应
        infinite_response = LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="tool_a",
                    arguments={"input": "test"},
                ),
            ],
        )

        tool = MockTool(name="tool_a")
        agent = AgentCore(
            config=mock_config,
            tools=[tool],
            max_iterations=3,
        )

        with patch.object(
            agent._llm_client,
            "chat_with_tools",
            new_callable=AsyncMock,
            return_value=infinite_response,
        ):
            result = await agent.process_input("无限循环测试")

        assert "迭代次数" in result

    @pytest.mark.asyncio
    async def test_process_input_empty_response(self, agent):
        """测试空响应处理"""
        empty_response = LLMResponse(
            content=None,
            tool_calls=[],
        )

        with patch.object(
            agent._llm_client,
            "chat_with_tools",
            new_callable=AsyncMock,
            return_value=empty_response,
        ):
            result = await agent.process_input("测试空响应")

        assert "无法处理" in result


# ============== 上下文管理器测试 ==============


class TestAsyncContextManager:
    """测试异步上下文管理器"""

    @pytest.mark.asyncio
    async def test_async_context_manager(self, mock_config):
        """测试异步上下文管理器"""
        async with AgentCore(config=mock_config) as agent:
            assert agent is not None
            assert isinstance(agent, AgentCore)
        # 退出时应该调用 close

    @pytest.mark.asyncio
    async def test_close_method(self, agent):
        """测试关闭方法"""
        await agent.close()
        # 不应该抛出异常


# ============== 集成测试 ==============


class TestAgentIntegration:
    """集成测试"""

    @pytest.mark.asyncio
    async def test_full_conversation_flow(self, mock_config):
        """测试完整对话流程"""
        # 创建一个工具
        tool = MockTool(
            name="add_event",
            execute_result=ToolResult(
                success=True,
                message="事件已创建",
                data={"event_id": "evt_123", "title": "测试会议"},
            ),
        )

        agent = AgentCore(
            config=mock_config,
            tools=[tool],
        )

        # 模拟 LLM 响应序列
        responses = [
            # 第一次：调用工具
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="add_event",
                        arguments={"title": "测试会议", "start_time": "明天下午3点"},
                    ),
                ],
            ),
            # 第二次：最终响应
            LLMResponse(
                content="已为您创建测试会议。",
                tool_calls=[],
            ),
        ]

        with patch.object(
            agent._llm_client,
            "chat_with_tools",
            new_callable=AsyncMock,
            side_effect=responses,
        ):
            result = await agent.process_input("帮我创建一个明天下午3点的测试会议")

        assert result == "已为您创建测试会议。"
        assert tool.execute_called is True

    @pytest.mark.asyncio
    async def test_multi_turn_conversation(self, mock_config):
        """测试多轮对话"""
        agent = AgentCore(config=mock_config)

        responses = [
            LLMResponse(content="你好！我是日程助手。", tool_calls=[]),
            LLMResponse(content="今天天气不错！", tool_calls=[]),
        ]

        with patch.object(
            agent._llm_client,
            "chat_with_tools",
            new_callable=AsyncMock,
            side_effect=responses,
        ):
            r1 = await agent.process_input("你好")
            r2 = await agent.process_input("今天天气怎么样？")

        assert r1 == "你好！我是日程助手。"
        assert r2 == "今天天气不错！"

        # 上下文应该包含 4 条消息（2 轮对话）
        assert len(agent.context) == 4

    @pytest.mark.asyncio
    async def test_cross_window_sync_resets_prompt_token_hint_for_timely_compression(
        self, mock_config
    ):
        """跨窗口同步到新增消息后，_last_prompt_tokens 应重置为 None 以确保后续压缩检查基于实际上下文重估。"""
        agent = AgentCore(config=mock_config, source="cli", user_id="root")
        await agent.activate_session("cli:shared")
        # 模拟另一窗口写入了同一 session 的新增消息
        agent._chat_history_db.write_message(
            session_id="cli:shared",
            role="assistant",
            content="外部窗口新增消息",
            source="cli",
        )
        agent._last_prompt_tokens = 999999

        with patch.object(
            agent._llm_client,
            "chat_with_tools",
            new_callable=AsyncMock,
            return_value=LLMResponse(content="ok", tool_calls=[]),
        ):
            await agent.process_input("test")

        assert agent._last_prompt_tokens is None or agent._last_prompt_tokens != 999999

    @pytest.mark.asyncio
    async def test_activate_session_with_zero_replay_does_not_crash_with_existing_history(
        self, mock_config
    ):
        """当有历史且 replay_messages_limit=0 时，不应触发索引异常。"""
        agent = AgentCore(config=mock_config, source="cli", user_id="root")
        sid = "cli:replay-zero"
        agent._chat_history_db.write_message(
            session_id=sid, role="user", content="u1", source="cli"
        )
        agent._chat_history_db.write_message(
            session_id=sid, role="assistant", content="a1", source="cli"
        )

        await agent.activate_session(sid, replay_messages_limit=0)

        assert len(agent.context) == 0
        assert agent.get_turn_count() == 0
