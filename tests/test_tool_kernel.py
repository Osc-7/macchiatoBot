"""
Agent Kernel 工具分层测试。
"""

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.config import AgentConfig, Config, LLMConfig
from agent_core.agent import AgentCore
from agent_core.llm import LLMResponse, ToolCall
from agent_core.orchestrator import ToolWorkingSetManager
from agent_core.kernel_interface.profile import CoreProfile
from agent_core.mcp.proxy_tool import MCPProxyTool
from agent_core.tools import (
    BaseTool,
    CallToolTool,
    SearchToolsTool,
    ToolDefinition,
    ToolParameter,
    ToolResult,
    VersionedToolRegistry,
)


def test_mcp_openai_safe_local_name():
    from agent_core.mcp.client import mcp_openai_safe_local_name

    assert mcp_openai_safe_local_name("tavily", "tavily_search") == "tavily__tavily_search"
    assert mcp_openai_safe_local_name("a.b", "c") == "a_b__c"


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

    def test_search_name_prefix(self):
        registry = VersionedToolRegistry()
        registry.register(DummyTool(name="tavily__alpha", description="a"))
        registry.register(DummyTool(name="tavily__beta", description="b"))
        registry.register(DummyTool(name="get_tasks", description="任务"))

        results = registry.search(
            query="", limit=10, name_prefix="tavily__", tags=None
        )
        names = [item["name"] for item in results]
        assert set(names) == {"tavily__alpha", "tavily__beta"}

    def test_search_hits_tool_tags_via_query(self):
        """工具 tags 参与语料：用户只写 query 也能命中仅 tag 含关键词的工具。"""
        registry = VersionedToolRegistry()
        registry.register(
            DummyTool(
                name="obscure_tool",
                description="描述里不出现日程二字",
                tags=["日程", "同步"],
            )
        )
        results = registry.search("日程", limit=5)
        assert any(r["name"] == "obscure_tool" for r in results)

    def test_search_weak_match_when_no_keyword_hit(self):
        registry = VersionedToolRegistry()
        registry.register(DummyTool(name="get_tasks", description="查询任务列表"))
        results = registry.search("totally_unrelated_xyz", limit=5)
        assert len(results) >= 1
        assert all(r.get("weak_match") is True for r in results)

    def test_search_matches_usage_notes(self):
        registry = VersionedToolRegistry()
        registry.register(
            DummyTool(
                name="plain",
                description="无关",
            )
        )
        proxy = MCPProxyTool(
            manager=MagicMock(),
            local_name="discourse__list_topics",
            server_name="discourse",
            remote_name="list_topics",
            description="列出话题",
            input_schema={"type": "object", "properties": {}},
        )
        registry.register(proxy)
        results = registry.search("MCP Server", limit=5)
        names = [item["name"] for item in results]
        assert "discourse__list_topics" in names


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
    async def test_search_tools_name_prefix_and_tool_source(self):
        registry = VersionedToolRegistry()
        registry.register(DummyTool(name="tavily__builtin", description="x"))
        proxy = MCPProxyTool(
            manager=MagicMock(),
            local_name="tavily__z",
            server_name="tavily",
            remote_name="z",
            description="z tool",
            input_schema={"type": "object", "properties": {}},
        )
        registry.register(proxy)
        working_set = ToolWorkingSetManager(
            pinned_tools=["search_tools", "call_tool"],
            working_set_size=5,
        )
        tool = SearchToolsTool(registry=registry, working_set=working_set)
        result = await tool.execute(name_prefix="tavily__")
        assert result.success
        names = [t["name"] for t in result.data["tools"]]
        assert "tavily__z" in names
        row = next(r for r in result.data["tools"] if r["name"] == "tavily__z")
        assert row["tool_source"] == "mcp"
        assert row["mcp_server"] == "tavily"
        plain = next(r for r in result.data["tools"] if r["name"] == "tavily__builtin")
        assert plain["tool_source"] == "native"
        assert plain.get("mcp_server") is None

    @pytest.mark.asyncio
    async def test_search_tools_marks_uncallable_results(self):
        registry = VersionedToolRegistry()
        registry.register(DummyTool(name="secret_tool", description="秘密能力"))
        working_set = ToolWorkingSetManager(
            pinned_tools=["search_tools", "call_tool", "bash"],
            working_set_size=3,
        )
        profile = CoreProfile(
            mode="full",
            allowed_tools=["search_tools", "call_tool", "bash"],
        )
        tool = SearchToolsTool(
            registry=registry,
            working_set=working_set,
            profile_getter=lambda: profile,
        )

        result = await tool.execute(query="秘密")
        assert result.success is True
        assert result.data["tools"][0]["callable_in_current_core"] is False
        assert result.data["tools"][0]["reason_if_denied"] is not None

        snapshot = working_set.build_snapshot(registry)
        assert "secret_tool" not in snapshot.tool_names

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
                working_set_size=2,
                max_iterations=6,
            ),
        )
        hidden_tool = DummyTool(name="hidden_tool", description="隐藏能力测试")
        profile = CoreProfile(
            mode="full",
            allowed_tools=["search_tools", "call_tool", "bash", "hidden_tool"],
            tool_template="default",
            tool_exposure_mode="empty",
            allow_dangerous_commands=True,
        )
        agent = AgentCore(
            config=config,
            tools=[hidden_tool],
            max_iterations=6,
            core_profile=profile,
        )

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
