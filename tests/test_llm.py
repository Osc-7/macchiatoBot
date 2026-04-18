"""
LLM 客户端测试
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
)

from agent_core.llm import LLMClient, LLMResponse, ToolCall
from agent_core.llm.client import _strip_thinking_content
from agent_core.llm.providers.openai_compat import (
    inject_gemini_dummy_thought_signatures_in_messages,
)
from agent_core.config import Config, LLMConfig
from agent_core.context import ConversationContext


def _install_mock_openai_client(client: LLMClient, mock_client) -> None:
    """把 LLMClient 路由到的每个 provider 底层 AsyncOpenAI 换成 mock。

    兼容旧测试直接操作 ``client._client`` 的风格。
    """
    for provider in client._providers.values():
        provider._client = mock_client


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
    tool_call.function.arguments = (
        '{"title": "测试事件", "start_time": "2026-02-18 10:00"}'
    )

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

    def test_tool_call_extra_content_optional(self):
        """extra_content 可选，用于 Gemini/Kimi 等 thought_signature 等多轮回传。"""
        tc = ToolCall(
            id="c1",
            name="f",
            arguments={},
            extra_content={"google": {"thought_signature": "x"}},
        )
        assert tc.extra_content["google"]["thought_signature"] == "x"


class TestGeminiDummyThoughtSignatures:
    """Gemini 文档：无真实签名时可用官方 dummy 通过校验（跨模型 / 中途切换）。"""

    def test_inject_fills_missing_signature(self):
        msgs = [
            {"role": "assistant", "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                }
            ]},
        ]
        out = inject_gemini_dummy_thought_signatures_in_messages(msgs)
        assert (
            out[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"]
            == "skip_thought_signature_validator"
        )

    def test_inject_skips_when_signature_present(self):
        msgs = [
            {"role": "assistant", "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                    "extra_content": {"google": {"thought_signature": "real"}},
                }
            ]},
        ]
        out = inject_gemini_dummy_thought_signatures_in_messages(msgs)
        assert out[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "real"


class TestConversationContextToolCalls:
    """上下文中的 tool_calls 须能携带厂商扩展字段。"""

    def test_extra_content_preserved_in_messages(self):
        ctx = ConversationContext()
        ctx.add_assistant_message(
            content=None,
            tool_calls=[
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                    "extra_content": {"google": {"thought_signature": "sig"}},
                }
            ],
        )
        msg = ctx.get_messages()[-1]
        assert msg["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "sig"


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


# 所有 LLMClient 测试都应当通过 openai_compat provider 层调用底层 AsyncOpenAI，
# 测试通过 patch `agent_core.llm.providers.openai_compat.AsyncOpenAI` 替换 SDK。
_OPENAI_PATCH = "agent_core.llm.providers.openai_compat.AsyncOpenAI"


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
        with patch(_OPENAI_PATCH) as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(
                return_value=mock_openai_response
            )
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=mock_config)
            _install_mock_openai_client(client, mock_client)

            response = await client.chat(
                messages=[{"role": "user", "content": "你好"}],
                system_message="你是一个助手",
            )

            assert response.content == "这是助手的回复"
            assert response.tool_calls == []
            assert response.finish_reason == "stop"

            call_args = mock_client.chat.completions.create.call_args
            assert call_args.kwargs["model"] == "ep-test-model"
            assert len(call_args.kwargs["messages"]) == 2

    @pytest.mark.asyncio
    async def test_chat_with_tools(self, mock_config, mock_openai_response_with_tools):
        """测试带工具的对话"""
        with patch(_OPENAI_PATCH) as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(
                return_value=mock_openai_response_with_tools
            )
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=mock_config)
            _install_mock_openai_client(client, mock_client)

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

            call_args = mock_client.chat.completions.create.call_args
            assert "tools" in call_args.kwargs
            assert call_args.kwargs["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_chat_with_tools_preserves_gemini_extra_content(self, mock_config):
        """OpenAI 兼容层在 tool_call 上可附带 extra_content.thought_signature（Gemini/Kimi 等），须解析并保留。"""
        tc = ChatCompletionMessageToolCall.model_validate(
            {
                "id": "call_sig",
                "type": "function",
                "function": {
                    "name": "request_permission",
                    "arguments": "{}",
                },
                "extra_content": {
                    "google": {"thought_signature": "test-signature-blob"},
                },
            }
        )
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = None
        response.choices[0].message.tool_calls = [tc]
        response.choices[0].finish_reason = "tool_calls"
        response.usage = None

        with patch(_OPENAI_PATCH) as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(return_value=response)
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=mock_config)
            _install_mock_openai_client(client, mock_client)

            out = await client.chat_with_tools(
                messages=[{"role": "user", "content": "hi"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "request_permission",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )

        assert len(out.tool_calls) == 1
        assert out.tool_calls[0].extra_content == {
            "google": {"thought_signature": "test-signature-blob"},
        }

    @pytest.mark.asyncio
    async def test_chat_with_image(self, mock_config):
        """测试多模态识图请求参数构造"""
        with patch(_OPENAI_PATCH) as mock_openai:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "图片里有一段报错信息"
            mock_response.choices[0].finish_reason = "stop"
            mock_response.usage = None
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=mock_config)
            _install_mock_openai_client(client, mock_client)

            response = await client.chat_with_image(
                prompt="提取错误信息",
                image_url="https://example.com/error.png",
                system_message="你是视觉助手",
                model_override="qwen-vl-max-latest",
            )

            assert response.content == "图片里有一段报错信息"
            call_args = mock_client.chat.completions.create.call_args
            assert call_args.kwargs["model"] == "qwen-vl-max-latest"
            user_msg = call_args.kwargs["messages"][-1]
            assert user_msg["role"] == "user"
            assert user_msg["content"][0]["type"] == "text"
            assert user_msg["content"][1]["type"] == "image_url"

    @pytest.mark.asyncio
    async def test_chat_without_tools(self, mock_config, mock_openai_response):
        """测试不带工具的 chat_with_tools"""
        with patch(_OPENAI_PATCH) as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(
                return_value=mock_openai_response
            )
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=mock_config)
            _install_mock_openai_client(client, mock_client)

            response = await client.chat_with_tools(
                messages=[{"role": "user", "content": "你好"}],
                tools=None,
            )

            assert response.content == "这是助手的回复"

            call_args = mock_client.chat.completions.create.call_args
            assert "tools" not in call_args.kwargs

    @pytest.mark.asyncio
    async def test_close(self, mock_config):
        """测试关闭客户端"""
        with patch(_OPENAI_PATCH) as mock_openai:
            mock_client = AsyncMock()
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=mock_config)
            _install_mock_openai_client(client, mock_client)
            await client.close()

            mock_client.close.assert_called()

    @pytest.mark.asyncio
    async def test_vendor_params_passed_as_extra_body(self):
        """vendor_params 原样作为 extra_body 发送"""
        vp = {
            "enable_search": True,
            "search_options": {
                "forced_search": True,
                "search_strategy": "max",
                "enable_source": True,
            },
        }
        config = Config(
            llm=LLMConfig(
                provider="qwen",
                api_key="test-api-key",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model="qwen-plus",
                temperature=0.7,
                max_tokens=4096,
                vendor_params=vp,
            )
        )

        with patch(_OPENAI_PATCH) as mock_openai:
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
            _install_mock_openai_client(client, mock_client)

            response = await client.chat_with_tools(
                messages=[{"role": "user", "content": "杭州天气如何"}],
                tools=None,
            )

            assert response.content == "这是回复"

            call_args = mock_client.chat.completions.create.call_args
            assert call_args.kwargs["extra_body"] == vp

    @pytest.mark.asyncio
    async def test_vendor_params_empty_no_extra_body(self):
        """vendor_params 为空时不传 extra_body"""
        config = Config(
            llm=LLMConfig(
                provider="qwen",
                api_key="test-api-key",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model="qwen-plus",
                temperature=0.7,
                max_tokens=4096,
                vendor_params={},
            )
        )

        with patch(_OPENAI_PATCH) as mock_openai:
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
            _install_mock_openai_client(client, mock_client)

            response = await client.chat_with_tools(
                messages=[{"role": "user", "content": "你好"}],
                tools=None,
            )

            assert response.content == "这是回复"

            call_args = mock_client.chat.completions.create.call_args
            assert "extra_body" not in call_args.kwargs

    @pytest.mark.asyncio
    async def test_parallel_tool_calls_respects_config(self):
        """parallel_tool_calls=false 时不传该参数"""
        config = Config(
            llm=LLMConfig(
                api_key="k",
                model="m",
                parallel_tool_calls=False,
            )
        )
        tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]

        with patch(_OPENAI_PATCH) as mock_openai:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = None
            mock_response.choices[0].message.tool_calls = None
            mock_response.choices[0].finish_reason = "stop"
            mock_response.usage = None
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=config)
            _install_mock_openai_client(client, mock_client)

            await client.chat_with_tools(
                messages=[{"role": "user", "content": "hi"}],
                tools=tools,
            )

            call_args = mock_client.chat.completions.create.call_args
            assert "parallel_tool_calls" not in call_args.kwargs

    def test_context_window_property(self):
        """context_window 配置优先于模型名推断"""
        c1 = Config(
            llm=LLMConfig(api_key="k", model="gpt-4o-mini", context_window=50000)
        )
        assert LLMClient(config=c1).context_window == 50000
        c2 = Config(llm=LLMConfig(api_key="k", model="qwen3.5-plus"))
        assert LLMClient(config=c2).context_window == 1_000_000

    @pytest.mark.asyncio
    async def test_reasoning_content_stream(self):
        """流式响应汇总 reasoning_content"""

        async def fake_stream():
            chunk1 = MagicMock()
            chunk1.choices = [MagicMock()]
            chunk1.choices[0].delta = MagicMock()
            chunk1.choices[0].delta.content = None
            chunk1.choices[0].delta.reasoning_content = "思"
            chunk1.choices[0].finish_reason = None

            chunk2 = MagicMock()
            chunk2.choices = [MagicMock()]
            chunk2.choices[0].delta = MagicMock()
            chunk2.choices[0].delta.content = "答"
            chunk2.choices[0].delta.reasoning_content = "考"
            chunk2.choices[0].finish_reason = "stop"

            yield chunk1
            yield chunk2

        config = Config(llm=LLMConfig(api_key="k", model="m", stream=True))
        with patch(_OPENAI_PATCH) as mock_openai:
            mock_client = AsyncMock()
            mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=config)
            _install_mock_openai_client(client, mock_client)

            response = await client.chat_with_tools(
                messages=[{"role": "user", "content": "hi"}],
                tools=None,
            )
            assert response.reasoning_content == "思考"
            assert response.content == "答"

    @pytest.mark.asyncio
    async def test_reasoning_content_non_stream(self):
        """非流式响应解析 reasoning_content"""
        config = Config(
            llm=LLMConfig(api_key="k", model="m", stream=False),
        )
        with patch(_OPENAI_PATCH) as mock_openai:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            msg = mock_response.choices[0].message
            msg.content = "最终"
            msg.tool_calls = None
            msg.reasoning_content = "推理过程"
            mock_response.choices[0].finish_reason = "stop"
            mock_response.usage = None
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_openai.return_value = mock_client

            client = LLMClient(config=config)
            _install_mock_openai_client(client, mock_client)

            response = await client.chat_with_tools(
                messages=[{"role": "user", "content": "hi"}],
                tools=None,
            )
            assert response.reasoning_content == "推理过程"
            assert response.content == "最终"

    @pytest.mark.asyncio
    async def test_web_extractor_search_strategy_not_agent_max_in_vendor_params(self):
        """vendor_params 中若含 search_options，不应默认写成 agent_max（与工具冲突）"""
        config = Config(
            llm=LLMConfig(
                provider="qwen",
                api_key="test-api-key",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model="qwen-plus",
                temperature=0.7,
                max_tokens=4096,
                vendor_params={
                    "enable_search": True,
                    "enable_web_extractor": True,
                    "search_options": {"search_strategy": "turbo"},
                },
            )
        )

        with patch(_OPENAI_PATCH) as mock_openai:
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
            _install_mock_openai_client(client, mock_client)

            await client.chat_with_tools(
                messages=[{"role": "user", "content": "你好"}],
                tools=None,
            )

            extra_body = mock_client.chat.completions.create.call_args.kwargs["extra_body"]
            assert extra_body["search_options"].get("search_strategy") != "agent_max"


class TestLLMClientIntegration:
    """LLM 客户端集成测试（需要真实 API）"""

    @pytest.mark.skip(reason="需要真实 API Key，跳过测试")
    @pytest.mark.asyncio
    async def test_real_chat(self):
        """测试真实 API 调用（跳过）"""
        from agent_core.config import get_config
        from agent_core.llm import LLMClient

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
