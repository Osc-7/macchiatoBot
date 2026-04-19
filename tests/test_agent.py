"""
Agent 测试用例

测试 AgentCore 的核心功能。
"""

import json
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.config import AgentConfig, Config, LLMConfig, MCPConfig
from agent_core.agent import AgentCore
from agent_core.kernel_interface.profile import CoreProfile
from agent_core.context import ConversationContext
from agent_core.llm import LLMResponse, ToolCall
from agent_core.tools import (
    BaseTool,
    ToolDefinition,
    ToolParameter,
    ToolResult,
    VersionedToolRegistry,
)


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
        # 2 custom + 3 chat + search_tools + call_tool + request_permission + ask_user（meta 全 Core 注册）
        assert len(agent.tool_registry) == 9

    def test_init_without_tools(self, mock_config):
        """测试不传入工具时初始化"""
        agent = AgentCore(config=mock_config)

        # 3 chat + search_tools + call_tool + request_permission + ask_user
        assert len(agent.tool_registry) == 7
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
        # 2 original + 1 new + 3 chat + meta（含 request_permission、ask_user）
        assert len(agent.tool_registry) == 10

    def test_unregister_tool(self, agent):
        """测试注销工具"""
        result = agent.unregister_tool("tool_a")

        assert result is True
        assert not agent.tool_registry.has("tool_a")
        # 2 original - 1 removed + 3 chat + meta
        assert len(agent.tool_registry) == 8

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

    def test_build_system_prompt_sub_vs_background_loader_mode(
        self, mock_config, mock_tools
    ):
        """sub 与主会话同为 full；仅 background 走 minimal。"""
        sub = AgentCore(
            config=mock_config,
            tools=mock_tools,
            max_iterations=3,
            core_profile=CoreProfile(mode="sub"),
        )
        sub_prompt = sub._build_system_prompt()
        assert "# IDENTITY" in sub_prompt
        assert "# Workspace Files" in sub_prompt

        bg = AgentCore(
            config=mock_config,
            tools=mock_tools,
            max_iterations=3,
            core_profile=CoreProfile(mode="background"),
        )
        bg_prompt = bg._build_system_prompt()
        assert "# IDENTITY" not in bg_prompt
        assert "# Workspace Files" not in bg_prompt
        assert "当前时间上下文" in bg_prompt
        assert "# 工具使用" in bg_prompt

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


class TestMcpToolCatalogSync:
    """MCP 代理工具须进入 tool_catalog，供 Kernel 下 search_tools / call_tool 发现。"""

    @pytest.mark.asyncio
    async def test_mcp_proxies_merged_into_tool_catalog(self, mock_config):
        mock_config.mcp = MCPConfig(enabled=True, servers=[])
        cat = VersionedToolRegistry()
        proxy = MockTool(name="tavily.stub_search")

        with patch("agent_core.agent.agent.MCPClientManager") as MCM:
            mgr = MagicMock()
            MCM.return_value = mgr
            mgr.connect = AsyncMock()
            mgr.get_proxy_tools = MagicMock(return_value=[proxy])
            mgr.close = AsyncMock()

            async with AgentCore(
                config=mock_config,
                tools=[MockTool(name="ordinary")],
                tool_catalog=cat,
            ) as agent:
                assert agent._tool_registry.has("tavily.stub_search")
                assert cat.has(
                    "tavily.stub_search"
                ), "search_tools/call_tool 使用 tool_catalog 时必须能解析 MCP 工具名"


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


# ============== 上下文压缩阈值测试 ==============


class TestCompressThreshold:
    """``AgentCore._compute_compress_threshold`` 的单元测试。

    覆盖三个上限的 ``min`` 组合，以及切换模型时按 context_window 比例自适应的行为。
    """

    @staticmethod
    def _make_agent(
        *,
        max_working_tokens: int,
        context_window_ratio: Optional[float],
        context_window: Optional[int],
        profile_max: Optional[int] = None,
    ):
        """构造最小可用 AgentCore，避开真实 LLM 与磁盘 IO。"""
        cfg = Config(
            llm=LLMConfig(api_key="k", model="test-model"),
            agent=AgentConfig(max_iterations=1),
        )
        cfg.memory.enabled = False
        cfg.memory.max_working_tokens = max_working_tokens
        cfg.memory.context_window_ratio = context_window_ratio

        profile = (
            CoreProfile(max_context_tokens=profile_max)
            if profile_max is not None
            else None
        )

        with patch("agent_core.agent.agent.LLMClient"):
            agent = AgentCore(config=cfg, tools=[], core_profile=profile)

        # 用属性 mock 替换 LLMClient 的 context_window，让测试可控；
        # None 表示模拟 provider 暂未声明窗口。
        type(agent._llm_client).context_window = property(  # type: ignore[assignment]
            lambda _self, _cw=context_window: _cw if _cw is not None else 0
        )
        return agent

    def test_threshold_uses_min_of_absolute_and_ratio_window(self):
        """ratio*window < max_working_tokens 时应以前者为阈值。"""
        agent = self._make_agent(
            max_working_tokens=1_000_000,
            context_window_ratio=0.5,
            context_window=200_000,
        )
        assert agent._compute_compress_threshold() == 100_000

    def test_threshold_falls_back_to_absolute_when_ratio_disabled(self):
        """ratio=None 应只看绝对上限，与窗口大小无关。"""
        agent = self._make_agent(
            max_working_tokens=8_000,
            context_window_ratio=None,
            context_window=1_000_000,
        )
        assert agent._compute_compress_threshold() == 8_000

    def test_threshold_falls_back_to_absolute_when_window_unknown(self):
        """provider 未声明 context_window（返回 0）时跳过比例项。"""
        agent = self._make_agent(
            max_working_tokens=50_000,
            context_window_ratio=0.75,
            context_window=None,
        )
        assert agent._compute_compress_threshold() == 50_000

    def test_threshold_respects_profile_max_context_tokens(self):
        """profile.max_context_tokens 进一步收紧（子 Agent 场景）。"""
        agent = self._make_agent(
            max_working_tokens=200_000,
            context_window_ratio=None,
            context_window=1_000_000,
            profile_max=40_000,
        )
        assert agent._compute_compress_threshold() == 40_000

    def test_threshold_takes_min_across_all_three(self):
        """三路上限同时存在，最终取最小那一路。"""
        agent = self._make_agent(
            max_working_tokens=300_000,
            context_window_ratio=0.8,
            context_window=200_000,  # ratio*window=160_000
            profile_max=120_000,
        )
        assert agent._compute_compress_threshold() == 120_000

    def test_threshold_shrinks_after_model_switch_to_smaller_window(self):
        """模拟运行时 /model 切换：context_window 变小后，阈值自动收紧。"""
        agent = self._make_agent(
            max_working_tokens=1_000_000,
            context_window_ratio=0.75,
            context_window=1_000_000,
        )
        # 切换前：ratio*1M = 750_000
        assert agent._compute_compress_threshold() == 750_000

        # 模拟 switch_model 后，活跃 provider 窗口变成 200k
        type(agent._llm_client).context_window = property(  # type: ignore[assignment]
            lambda _self: 200_000
        )
        # 切换后：ratio*200k = 150_000，应立即生效（无需重建 WorkingMemory）
        assert agent._compute_compress_threshold() == 150_000


# ============== /compress 手动压缩测试 ==============


class TestManualCompressContext:
    """``AgentCore.compress_context``：``/compress`` 命令路径的端到端单测。

    覆盖：
    - 有 summary LLM 时正常折叠并返回结构化结果；
    - 无 summary LLM 时仅截断；
    - 消息数不足以折叠时的幂等返回；
    - 压缩后 ``_last_prompt_tokens`` 重置（避免下一轮阈值判断误用旧值）。
    """

    @staticmethod
    def _make_agent(*, with_summary_llm: bool, max_working_tokens: int = 200_000):
        cfg = Config(
            llm=LLMConfig(api_key="k", model="test-model"),
            agent=AgentConfig(max_iterations=1),
        )
        cfg.memory.enabled = False
        cfg.memory.max_working_tokens = max_working_tokens

        with patch("agent_core.agent.agent.LLMClient"):
            agent = AgentCore(config=cfg, tools=[])

        type(agent._llm_client).context_window = property(  # type: ignore[assignment]
            lambda _self: 200_000
        )
        type(agent._llm_client).model = property(  # type: ignore[assignment]
            lambda _self: "test-model"
        )

        if with_summary_llm:
            llm = MagicMock()
            llm.chat = AsyncMock(
                return_value=MagicMock(content="折叠后的会话摘要")
            )
            agent._summary_llm_client = llm
        else:
            agent._summary_llm_client = None
        return agent

    def _seed_messages(self, agent, n_user_turns: int) -> None:
        """填充 n_user_turns 个 (user, assistant) 对到 context.messages。"""
        ctx = agent._context
        for i in range(n_user_turns):
            ctx.add_user_message(f"u{i}")
            ctx.add_assistant_message(content=f"a{i}")

    @pytest.mark.asyncio
    async def test_compress_with_summary_llm_returns_structured_result(self):
        agent = self._make_agent(with_summary_llm=True)
        self._seed_messages(agent, n_user_turns=8)
        agent._last_prompt_tokens = 12345

        res = await agent.compress_context(keep_recent_turns=2)

        assert res["compressed"] is True
        assert res["summary"] == "折叠后的会话摘要"
        assert res["summary_chars"] == len("折叠后的会话摘要")
        assert res["messages_before"] == 16
        # 摘要 user + 最近 2 轮 (user, assistant) = 5 条
        assert res["messages_after"] == 5
        assert res["kept"] == 5
        assert res["compression_round"] == 1
        # 当前模型字段从 LLMClient 透出，便于前端展示
        assert res["model"] == "test-model"
        # 阈值字段必须有，供 /compress 卡片展示
        assert res["threshold_tokens"] > 0
        # 上下文 messages 的首条应是注入的「[会话进行中摘要]」
        first = agent._context.get_messages()[0]
        assert first["role"] == "user"
        assert "[会话进行中摘要]" in first["content"]
        # 压缩后 _last_prompt_tokens 必须重置，否则下一轮阈值判断会用旧值
        assert agent._last_prompt_tokens is None

    @pytest.mark.asyncio
    async def test_compress_without_summary_llm_only_truncates(self):
        agent = self._make_agent(with_summary_llm=False)
        self._seed_messages(agent, n_user_turns=6)

        res = await agent.compress_context(keep_recent_turns=2)

        assert res["summary"] == ""
        assert res["summary_chars"] == 0
        assert res["messages_before"] == 12
        # 无摘要：保留最近 2 轮原文
        assert res["messages_after"] == 4
        # 即使没摘要，也算压缩生效（消息数变少）
        assert res["compressed"] is True
        # 截断后 messages 应以最近的 user 起头
        assert agent._context.get_messages()[0]["content"] == "u4"

    @pytest.mark.asyncio
    async def test_compress_is_idempotent_when_too_few_messages(self):
        """消息数 ≤ keep_recent*2 时直接返回原状态（不调 summary LLM）。"""
        agent = self._make_agent(with_summary_llm=True)
        self._seed_messages(agent, n_user_turns=2)  # 4 条 messages

        res = await agent.compress_context(keep_recent_turns=6)

        assert res["compressed"] is False
        assert res["messages_before"] == res["messages_after"] == 4
        assert res["summary"] == ""
        # 不应触发 LLM
        agent._summary_llm_client.chat.assert_not_called()


# ============== 自适应 completion 预算 ==============


class TestEffectiveMaxTokens:
    """``AgentCore._compute_effective_max_tokens``：按 prompt 大小动态收紧
    completion 预算，避免常量 ``max_tokens`` 把窗口顶爆。

    覆盖：
    - 窗口未知时返回 None（按 provider 构造期固定值走）；
    - prompt 留有大量余量时取 ``min(配置 max_tokens, budget)``；
    - prompt 占满窗口时退化到最小预算 256，不返回 0；
    - 切换到小窗口模型后预算自动收紧。
    """

    @staticmethod
    def _make_agent(*, configured_max: int = 65536):
        cfg = Config(
            llm=LLMConfig(api_key="k", model="test-model", max_tokens=configured_max),
            agent=AgentConfig(max_iterations=1),
        )
        cfg.memory.enabled = False
        with patch("agent_core.agent.agent.LLMClient"):
            agent = AgentCore(config=cfg, tools=[])
        return agent

    @staticmethod
    def _set_window(agent, *, window: int, max_tokens: int):
        type(agent._llm_client).context_window = property(  # type: ignore[assignment]
            lambda _self, _cw=window: _cw
        )
        type(agent._llm_client).max_tokens = property(  # type: ignore[assignment]
            lambda _self, _m=max_tokens: _m
        )

    @staticmethod
    def _payload(*, system: str = "", messages=None, tools=None):
        p = MagicMock()
        p.system = system
        p.messages = messages or []
        p.tools = tools or []
        return p

    def test_returns_none_when_window_unknown(self):
        agent = self._make_agent()
        self._set_window(agent, window=0, max_tokens=65536)
        assert agent._compute_effective_max_tokens(self._payload()) is None

    def test_clamps_to_configured_max_when_budget_is_larger(self):
        """prompt 很小、budget >> 配置 max_tokens 时，返回配置值（不放大）。"""
        agent = self._make_agent(configured_max=4_000)
        self._set_window(agent, window=128_000, max_tokens=4_000)
        out = agent._compute_effective_max_tokens(
            self._payload(system="hi", messages=[{"role": "user", "content": "x"}])
        )
        assert out == 4_000

    def test_shrinks_when_prompt_takes_most_of_window(self):
        """prompt 占走大半窗口时，budget < 配置 max_tokens，应返回 budget。"""
        agent = self._make_agent(configured_max=65536)
        self._set_window(agent, window=128_000, max_tokens=65536)
        # 构造约 100k tokens 的 messages
        big = "abcd" * 25_000  # ~25k tokens
        msgs = [{"role": "user", "content": big} for _ in range(4)]  # ~100k
        out = agent._compute_effective_max_tokens(self._payload(messages=msgs))
        assert out is not None
        assert out < 65536, f"预算应被收紧到窗口剩余空间，实际 {out}"
        assert out > 256

    def test_falls_back_to_min_floor_when_prompt_overflows(self):
        """prompt 已经接近/超过窗口时，预算退化为 256（不返回 0/负数）。"""
        agent = self._make_agent(configured_max=4_000)
        self._set_window(agent, window=8_000, max_tokens=4_000)
        big = "abcd" * 10_000  # ~10k tokens > window
        out = agent._compute_effective_max_tokens(
            self._payload(messages=[{"role": "user", "content": big}])
        )
        assert out == 256

    def test_window_switch_shrinks_budget(self):
        """模拟运行时切换到小窗口模型，预算应自动收紧。"""
        agent = self._make_agent(configured_max=65536)
        msgs = [{"role": "user", "content": "abcd" * 10_000}]  # ~10k tokens

        self._set_window(agent, window=1_000_000, max_tokens=65536)
        big_window_budget = agent._compute_effective_max_tokens(self._payload(messages=msgs))

        self._set_window(agent, window=64_000, max_tokens=65536)
        small_window_budget = agent._compute_effective_max_tokens(self._payload(messages=msgs))

        assert big_window_budget == 65536
        assert small_window_budget is not None
        assert small_window_budget < big_window_budget


# ============== 入场截断在 run_loop 中的集成 ==============


class TestRunLoopToolResultOverflow:
    """验证 ``AgentCore.run_loop`` 在 ``add_tool_result`` 前正确触发入场截断。

    完全在内存中跑 mock 的 LLM + tool，确认 messages 中的 tool result 已被截断、
    完整内容已落盘到 ``{workspace_dir}/.tool_results/``。
    """

    @pytest.mark.asyncio
    async def test_oversized_tool_result_is_truncated_and_persisted(self, tmp_path):
        from agent_core.config import CommandToolsConfig
        from agent_core.kernel_interface import (
            ContextOverflowAction,
            ReturnAction,
            ToolCallAction,
            ToolResultEvent,
        )
        from agent_core.memory.working_memory import estimate_tokens

        # 大返回的 tool —— 估算约 40k tokens 的 data
        big_text = "abcd" * 40_000  # ~40k tokens
        big_tool = MockTool(
            name="big_tool",
            execute_result=ToolResult(
                success=True, message="search done", data={"raw": big_text}
            ),
        )

        cfg = Config(
            llm=LLMConfig(api_key="k", model="test-model"),
            agent=AgentConfig(max_iterations=3),
            command_tools=CommandToolsConfig(
                enabled=False,
                workspace_base_dir=str(tmp_path / "workspace"),
                workspace_isolation_enabled=True,
            ),
        )
        cfg.memory.enabled = False
        cfg.memory.max_tool_result_tokens = 5_000

        with patch("agent_core.agent.agent.LLMClient"):
            agent = AgentCore(
                config=cfg,
                tools=[big_tool],
                user_id="alice",
                source="cli",
            )

        # 第一轮：调一次 big_tool；第二轮：返回最终回复
        tool_call_response = LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="call_big", name="big_tool", arguments={"input": "go"})
            ],
            finish_reason="tool_calls",
        )
        final_response = LLMResponse(content="done", tool_calls=[])

        # 让自适应 max_tokens 退路：context_window=0 时返回 None，省掉 patch
        type(agent._llm_client).context_window = property(  # type: ignore[assignment]
            lambda _self: 0
        )
        type(agent._llm_client).max_tokens = property(  # type: ignore[assignment]
            lambda _self: 4096
        )

        # 直接驱动 run_loop 协程，避免依赖完整 AgentKernel
        agent._context.add_user_message("trigger")
        gen = agent.run_loop(turn_id=1, hooks=None)
        # 1) 第一次 yield：LLM 调用前，patch chat_with_tools 返回 tool_call_response
        with patch.object(
            agent._llm_client,
            "chat_with_tools",
            new_callable=AsyncMock,
            side_effect=[tool_call_response, final_response],
        ):
            action = await gen.asend(None)
            # 期望先 yield ToolCallAction（首轮 token=0，不会触发压缩）
            assert isinstance(action, ToolCallAction), f"unexpected: {type(action)}"
            assert action.tool_name == "big_tool"

            # 2) 喂回 tool 执行结果
            action = await gen.asend(
                ToolResultEvent(
                    tool_call_id=action.tool_call_id,
                    result=big_tool._execute_result,
                )
            )
            # run_loop 应在第二轮回到 LLM；此时 messages 中的 tool result 应已截断
            assert isinstance(action, (ReturnAction, ContextOverflowAction))

        # 验证 messages 中的 tool result 已被截断
        msgs = agent._context.get_messages()
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        tool_content = tool_msgs[0]["content"]
        # 截断后的 content 估算 token 应受 max_tool_result_tokens 约束
        assert estimate_tokens(tool_content) <= 5_000, (
            f"tool message content estimate={estimate_tokens(tool_content)} 超过上限 5000"
        )
        # 应含截断 marker（指向工作区相对路径）
        assert "已截断" in tool_content
        assert ".tool_results/" in tool_content

        # 验证完整内容已落盘到 {workspace_base}/cli/alice/.tool_results/
        overflow_dir = tmp_path / "workspace" / "cli" / "alice" / ".tool_results"
        assert overflow_dir.is_dir()
        files = list(overflow_dir.glob("*_big_tool_*.json"))
        assert len(files) == 1, f"应落盘一份完整 JSON，实际 {files}"
        full = json.loads(files[0].read_text(encoding="utf-8"))
        assert full["data"]["raw"] == big_text, "落盘内容应是原始未截断的 data"

    @pytest.mark.asyncio
    async def test_small_tool_result_not_truncated(self, tmp_path):
        from agent_core.config import CommandToolsConfig
        from agent_core.kernel_interface import (
            ContextOverflowAction,
            ReturnAction,
            ToolCallAction,
            ToolResultEvent,
        )

        small_tool = MockTool(
            name="small_tool",
            execute_result=ToolResult(
                success=True, message="done", data={"x": "tiny"}
            ),
        )
        cfg = Config(
            llm=LLMConfig(api_key="k", model="test-model"),
            agent=AgentConfig(max_iterations=3),
            command_tools=CommandToolsConfig(
                enabled=False,
                workspace_base_dir=str(tmp_path / "workspace"),
                workspace_isolation_enabled=True,
            ),
        )
        cfg.memory.enabled = False
        cfg.memory.max_tool_result_tokens = 5_000

        with patch("agent_core.agent.agent.LLMClient"):
            agent = AgentCore(
                config=cfg, tools=[small_tool], user_id="bob", source="cli"
            )
        type(agent._llm_client).context_window = property(  # type: ignore[assignment]
            lambda _self: 0
        )
        type(agent._llm_client).max_tokens = property(  # type: ignore[assignment]
            lambda _self: 4096
        )

        agent._context.add_user_message("hi")
        gen = agent.run_loop(turn_id=1, hooks=None)
        with patch.object(
            agent._llm_client,
            "chat_with_tools",
            new_callable=AsyncMock,
            side_effect=[
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(id="c1", name="small_tool", arguments={"input": "x"})
                    ],
                    finish_reason="tool_calls",
                ),
                LLMResponse(content="ok", tool_calls=[]),
            ],
        ):
            action = await gen.asend(None)
            assert isinstance(action, ToolCallAction)
            action = await gen.asend(
                ToolResultEvent(
                    tool_call_id=action.tool_call_id,
                    result=small_tool._execute_result,
                )
            )
            assert isinstance(action, (ReturnAction, ContextOverflowAction))

        # 未触发：不应有落盘文件
        overflow_dir = tmp_path / "workspace" / "cli" / "bob" / ".tool_results"
        assert not overflow_dir.exists() or not any(overflow_dir.iterdir())
        # tool message 内容里也不应有截断标记
        tool_msgs = [m for m in agent._context.get_messages() if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "已截断" not in tool_msgs[0]["content"]
