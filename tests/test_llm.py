"""
LLM 客户端测试
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from schedule_agent.core.llm import LLMClient, LLMResponse, ToolCall
from schedule_agent.core.llm.client import _strip_thinking_content
from schedule_agent.config import Config, LLMConfig, SearchOptionsConfig


class TestStripThinkingContent:
    """测试 Qwen 思考内容剥离"""

    def test_no_think_tag(self):
        """无 think 标签时原样返回"""
        assert _strip_thinking_content("直接回复") == "直接回复"

    def test_with_think_tag(self):
        """有 <think> 块时只保留其后内容"""
        raw = "好的，让我总结一下。\n\n**日程：**...\n</think>\n\n看看你今天的安排～"
        assert _strip_thinking_content(raw) == "看看你今天的安排～"

    def test_empty_after_strip(self):
        """</think> 后为空时返回空字符串"""
        raw = "思考内容</think>"
        assert _strip_thinking_content(raw) == ""

    def test_none_input(self):
        """None 输入返回 None"""
        assert _strip_thinking_content(None) is None

    def test_empty_string(self):
        """空字符串原样返回"""
        assert _strip_thinking_content("") == ""


@pytest.fixture
def mock_config():
    """创建模拟配置"""
    return Config(
        llm=LLMConfig(
            provider="doubao",
            api_key="test-api-key",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model="ep-test-model",
            temperature=0.7,
            max_tokens=4096,
        )
    )


@pytest.fixture
def mock_openai_response():
    """创建模拟 OpenAI 响应"""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "这是助手的回复"
    response.choices[0].message.tool_calls = None
    response.choices[0].finish_reason = "stop"
    return response


@pytest.fixture
def mock_openai_response_with_tools():
    """创建带工具调用的模拟响应"""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = None

    # 模拟工具调用
    tool_call = MagicMock()
    tool_call.id = "call_123"
    tool_call.function.name = "create_event"
    tool_call.function.arguments = '{"title": "测试事件", "start_time": "2026-02-18 10:00"}'

    response.choices[0].message.tool_calls = [tool_call]
    response.choices[0].finish_reason = "tool_calls"
    return response


class TestToolCall:
    """测试 ToolCall 数据类"""

    def test_tool_call_creation(self):
        """测试工具调用创建"""
        tool_call = ToolCall(
            id="call_123",
            name="create_event",
            arguments={"title": "测试事件"},
        )

        assert tool_call.id == "call_123"
        assert tool_call.name == "create_event"
        assert tool_call.arguments == {"title": "测试事件"}


class TestLLMResponse:
    """测试 LLMResponse 数据类"""

    def test_response_creation(self):
        """测试响应创建"""
        response = LLMResponse(
            content="这是回复",
            tool_calls=[],
            finish_reason="stop",
        )

        assert response.content == "这是回复"
        assert response.tool_calls == []
        assert response.finish_reason == "stop"

    def test_response_with_tool_calls(self):
        """测试带工具调用的响应"""
        tool_call = ToolCall(
            id="call_123",
            name="create_event",
            arguments={"title": "测试"},
        )

        response = LLMResponse(
            content=None,
            tool_calls=[tool_call],
            finish_reason="tool_calls",
        )

        assert response.content is None
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "create_event"


class TestLLMClient:
    """测试 LLMClient"""

    def test_client_initialization(self, mock_config):
        """测试客户端初始化"""
        client = LLMClient(config=mock_config)

        assert client.model == "ep-test-model"
        assert client.temperature == 0.7
        assert client.max_tokens == 4096

    @pytest.mark.asyncio
    async def test_chat_basic(self, mock_config, mock_openai_response):
        """测试基础对话"""
        with patch("schedule_agent.core.llm.client.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(
                return_value=mock_openai_response
            )
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=mock_config)
            client._client = mock_client

            response = await client.chat(
                messages=[{"role": "user", "content": "你好"}],
                system_message="你是一个助手",
            )

            assert response.content == "这是助手的回复"
            assert response.tool_calls == []
            assert response.finish_reason == "stop"

            # 验证调用参数
            call_args = mock_client.chat.completions.create.call_args
            assert call_args.kwargs["model"] == "ep-test-model"
            assert len(call_args.kwargs["messages"]) == 2  # system + user

    @pytest.mark.asyncio
    async def test_chat_with_tools(
        self, mock_config, mock_openai_response_with_tools
    ):
        """测试带工具的对话"""
        with patch("schedule_agent.core.llm.client.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(
                return_value=mock_openai_response_with_tools
            )
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=mock_config)
            client._client = mock_client

            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "create_event",
                        "description": "创建事件",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                            },
                        },
                    },
                }
            ]

            response = await client.chat_with_tools(
                messages=[{"role": "user", "content": "创建一个会议"}],
                tools=tools,
                system_message="你是一个日程助手",
            )

            assert response.content is None
            assert len(response.tool_calls) == 1
            assert response.tool_calls[0].name == "create_event"
            assert response.tool_calls[0].id == "call_123"

            # 验证调用参数
            call_args = mock_client.chat.completions.create.call_args
            assert "tools" in call_args.kwargs
            assert call_args.kwargs["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_chat_without_tools(self, mock_config, mock_openai_response):
        """测试不带工具的 chat_with_tools"""
        with patch("schedule_agent.core.llm.client.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(
                return_value=mock_openai_response
            )
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=mock_config)
            client._client = mock_client

            response = await client.chat_with_tools(
                messages=[{"role": "user", "content": "你好"}],
                tools=None,
            )

            assert response.content == "这是助手的回复"

            # 验证调用参数不包含 tools
            call_args = mock_client.chat.completions.create.call_args
            assert "tools" not in call_args.kwargs

    @pytest.mark.asyncio
    async def test_close(self, mock_config):
        """测试关闭客户端"""
        with patch("schedule_agent.core.llm.client.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=mock_config)
            await client.close()

            mock_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_with_web_search_enabled(self):
        """测试启用联网搜索功能"""
        config = Config(
            llm=LLMConfig(
                provider="qwen",
                api_key="test-api-key",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model="qwen-plus",
                temperature=0.7,
                max_tokens=4096,
                enable_search=True,
                search_options=SearchOptionsConfig(
                    forced_search=True,
                    search_strategy="max",
                    enable_source=True,
                ),
            )
        )

        with patch("schedule_agent.core.llm.client.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "这是回复"
            mock_response.choices[0].message.tool_calls = None
            mock_response.choices[0].finish_reason = "stop"
            mock_response.usage = None
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=config)
            client._client = mock_client

            response = await client.chat_with_tools(
                messages=[{"role": "user", "content": "杭州天气如何"}],
                tools=None,
            )

            assert response.content == "这是回复"

            # 验证 extra_body 中包含 enable_search
            call_args = mock_client.chat.completions.create.call_args
            assert "extra_body" in call_args.kwargs
            extra_body = call_args.kwargs["extra_body"]
            assert extra_body["enable_search"] is True
            assert "search_options" in extra_body
            assert extra_body["search_options"]["forced_search"] is True
            assert extra_body["search_options"]["search_strategy"] == "max"
            assert extra_body["search_options"]["enable_source"] is True

    @pytest.mark.asyncio
    async def test_chat_with_web_search_disabled(self):
        """测试禁用联网搜索功能"""
        config = Config(
            llm=LLMConfig(
                provider="qwen",
                api_key="test-api-key",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model="qwen-plus",
                temperature=0.7,
                max_tokens=4096,
                enable_search=False,  # 禁用联网搜索
            )
        )

        with patch("schedule_agent.core.llm.client.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "这是回复"
            mock_response.choices[0].message.tool_calls = None
            mock_response.choices[0].finish_reason = "stop"
            mock_response.usage = None
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=config)
            client._client = mock_client

            response = await client.chat_with_tools(
                messages=[{"role": "user", "content": "你好"}],
                tools=None,
            )

            assert response.content == "这是回复"

            # 验证 extra_body 不存在
            call_args = mock_client.chat.completions.create.call_args
            assert "extra_body" not in call_args.kwargs

    @pytest.mark.asyncio
    async def test_chat_with_web_search_non_qwen_provider(self):
        """测试非 Qwen 提供商不启用联网搜索"""
        config = Config(
            llm=LLMConfig(
                provider="doubao",  # 非 qwen 提供商
                api_key="test-api-key",
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                model="ep-test-model",
                temperature=0.7,
                max_tokens=4096,
                enable_search=True,  # 即使启用，非 qwen 也不应该传递
            )
        )

        with patch("schedule_agent.core.llm.client.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "这是回复"
            mock_response.choices[0].message.tool_calls = None
            mock_response.choices[0].finish_reason = "stop"
            mock_response.usage = None
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=config)
            client._client = mock_client

            response = await client.chat_with_tools(
                messages=[{"role": "user", "content": "你好"}],
                tools=None,
            )

            assert response.content == "这是回复"

            # 验证 extra_body 不存在（因为 provider 不是 qwen）
            call_args = mock_client.chat.completions.create.call_args
            assert "extra_body" not in call_args.kwargs

    @pytest.mark.asyncio
    async def test_chat_with_web_extractor_enabled(self):
        """测试：网页抓取功能已通过工具实现，全局不再启用"""
        # 注意：此测试已过时，网页抓取现在通过 WebExtractorTool 工具实现
        # 全局启用 enable_web_extractor 不再有效，应使用工具
        config = Config(
            llm=LLMConfig(
                provider="qwen",
                api_key="test-api-key",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model="qwen-plus",
                temperature=0.7,
                max_tokens=4096,
                enable_search=True,
                enable_web_extractor=True,  # 此配置不再在全局生效
            )
        )

        with patch("schedule_agent.core.llm.client.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "这是回复"
            mock_response.choices[0].message.tool_calls = None
            mock_response.choices[0].finish_reason = "stop"
            mock_response.usage = None
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=config)
            client._client = mock_client

            response = await client.chat_with_tools(
                messages=[{"role": "user", "content": "你好"}],
                tools=None,
            )

            assert response.content == "这是回复"

            # 验证：即使 enable_web_extractor=true，也不会在全局启用（避免与工具冲突）
            call_args = mock_client.chat.completions.create.call_args
            if "extra_body" in call_args.kwargs:
                extra_body = call_args.kwargs["extra_body"]
                # 不应该有 search_strategy: agent_max（因为会与工具冲突）
                if "search_options" in extra_body:
                    assert extra_body["search_options"].get("search_strategy") != "agent_max"

    @pytest.mark.asyncio
    async def test_chat_with_thinking_enabled(self):
        """测试启用思考模式（不启用网页抓取）"""
        config = Config(
            llm=LLMConfig(
                provider="qwen",
                api_key="test-api-key",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model="qwen-plus",
                temperature=0.7,
                max_tokens=4096,
                enable_search=True,
                enable_thinking=True,  # 仅启用思考模式
                enable_web_extractor=False,
            )
        )

        with patch("schedule_agent.core.llm.client.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "这是回复"
            mock_response.choices[0].message.tool_calls = None
            mock_response.choices[0].finish_reason = "stop"
            mock_response.usage = None
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=config)
            client._client = mock_client

            response = await client.chat_with_tools(
                messages=[{"role": "user", "content": "你好"}],
                tools=None,
            )

            assert response.content == "这是回复"

            # 验证 extra_body 中包含 enable_thinking，但不强制 search_strategy
            call_args = mock_client.chat.completions.create.call_args
            assert "extra_body" in call_args.kwargs
            extra_body = call_args.kwargs["extra_body"]
            assert extra_body["enable_search"] is True
            assert extra_body["enable_thinking"] is True
            # 如果没有设置 search_options，search_strategy 应该保持默认或配置值
            if "search_options" in extra_body:
                assert "search_strategy" not in extra_body["search_options"] or extra_body["search_options"].get("search_strategy") != "agent_max"


class TestLLMClientIntegration:
    """LLM 客户端集成测试（需要真实 API）"""
    @pytest.mark.skip(reason="需要真实 API Key，跳过测试")
    @pytest.mark.asyncio
    async def test_real_chat(self):
        """测试真实 API 调用（跳过）"""
        # 此测试需要真实的 API Key 和端点
        # 这里为真实 API 集成测试示例，需填写有效 KEY 后启用
        from schedule_agent.config import get_config
        from schedule_agent.core.llm import LLMClient

        config = get_config()
        client = LLMClient(config=config)
        user_message = {"role": "user", "content": "请用一句话介绍一下你自己。"}

        response = await client.chat_with_tools(
            messages=[user_message],
            tools=None,
        )

        assert response.content is not None
        assert isinstance(response.content, str)
        print("真实 LLM 回复:", response.content)
        await client.close()
        
