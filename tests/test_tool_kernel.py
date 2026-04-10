"""
Agent Kernel 工具分层测试。
"""

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, patch

import pytest

from agent_core.config import AgentConfig, Config, LLMConfig
from agent_core.agent import AgentCore
from agent_core.llm import LLMResponse, ToolCall
from agent_core.orchestrator import ToolWorkingSetManager
from agent_core.kernel_interface.profile import CoreProfile
from agent_core.tools import (
    BaseTool,
    CallToolTool,
    SearchToolsTool,
    ToolDefinition,
    ToolParameter,
    ToolResult,
    VersionedToolRegistry,
)


class DummyTool(BaseTool):
    def __init__(
        self,
        name: str = "dummy_tool",
        description: str = "dummy",
        tags: Optional[list[str]] = None,
    ):
        self._name = name
        self._description = description
        self._tags = tags or []
        self.called = False
        self.called_kwargs: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self._name,
            description=self._description,
            parameters=[
                ToolParameter(
                    name="input",
                    type="string",
                    description="输入",
                    required=False,
                )
            ],
            tags=self._tags,
        )

    async def execute(self, **kwargs) -> ToolResult:
        self.called = True
        self.called_kwargs = kwargs
        return ToolResult(success=True, message="ok", data={"kwargs": kwargs})


class TestVersionedRegistry:
    def test_search(self):
        registry = VersionedToolRegistry()
        registry.register(DummyTool(name="add_event", description="创建日程事件"))
        registry.register(DummyTool(name="get_tasks", description="查询任务"))

        results = registry.search("日程", limit=5)
        names = [item["name"] for item in results]
        assert "add_event" in names

    def test_search_tags_case_insensitive(self):
        registry = VersionedToolRegistry()
        registry.register(
            DummyTool(
                name="sync_canvas",
                description="同步 canvas 作业",
                tags=["canvas", "同步"],
            )
        )

        results = registry.search(query="", tags=["Canvas"], limit=5)
        names = [item["name"] for item in results]
        assert "sync_canvas" in names


class TestKernelTools:
    @pytest.mark.asyncio
    async def test_search_tools_updates_working_set(self):
        registry = VersionedToolRegistry()
        registry.register(DummyTool(name="get_tasks", description="查询任务列表"))
        working_set = ToolWorkingSetManager(
            pinned_tools=["search_tools", "call_tool"],
            working_set_size=3,
        )
        tool = SearchToolsTool(registry=registry, working_set=working_set)

        result = await tool.execute(query="任务")
        assert result.success is True
        assert result.data["count"] >= 1

        snapshot = working_set.build_snapshot(registry)
        assert "get_tasks" in snapshot.tool_names

    @pytest.mark.asyncio
    async def test_call_tool_executes_target(self):
        registry = VersionedToolRegistry()
        dummy = DummyTool(name="demo_tool")
        registry.register(dummy)

        caller = CallToolTool(registry=registry)
        result = await caller.execute(name="demo_tool", arguments={"input": "x"})
        assert result.success is True
        assert dummy.called is True
        assert dummy.called_kwargs == {"input": "x"}

    @pytest.mark.asyncio
    async def test_call_tool_inner_denied_when_profile_forbids(self):
        registry = VersionedToolRegistry()
        dummy = DummyTool(name="secret_tool")
        registry.register(dummy)
        profile = CoreProfile(
            mode="sub",
            allowed_tools=["call_tool"],
            allow_dangerous_commands=False,
        )
        caller = CallToolTool(
            registry=registry,
            profile_getter=lambda: profile,
        )
        result = await caller.execute(name="secret_tool", arguments={})
        assert result.success is False
        assert result.error == "PERMISSION_DENIED"
        assert dummy.called is False


class TestAgentKernelMode:
    @pytest.mark.asyncio
    async def test_agent_kernel_flow_search_then_call(self):
        config = Config(
            llm=LLMConfig(api_key="x", model="x"),
            agent=AgentConfig(
                tool_mode="kernel",
                pinned_tools=["search_tools", "call_tool"],
                working_set_size=2,
                max_iterations=6,
            ),
        )
        hidden_tool = DummyTool(name="hidden_tool", description="隐藏能力测试")
        agent = AgentCore(config=config, tools=[hidden_tool], max_iterations=6)

        responses = [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="search_tools",
                        arguments={"query": "隐藏"},
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="call_tool",
                        arguments={"name": "hidden_tool", "arguments": {"input": "ok"}},
                    )
                ],
            ),
            LLMResponse(content="完成", tool_calls=[]),
        ]

        with patch.object(
            agent._llm_client,
            "chat_with_tools",
            new_callable=AsyncMock,
            side_effect=responses,
        ) as mock_chat:
            output = await agent.process_input("执行隐藏能力")

        assert output == "完成"
        assert hidden_tool.called is True

        first_tools = mock_chat.call_args_list[0].kwargs["tools"]
        first_tool_names = [tool["function"]["name"] for tool in first_tools]
        assert "search_tools" in first_tool_names
        assert "call_tool" in first_tool_names
        assert "hidden_tool" not in first_tool_names

        second_tools = mock_chat.call_args_list[1].kwargs["tools"]
        second_tool_names = [tool["function"]["name"] for tool in second_tools]
        assert "hidden_tool" in second_tool_names
