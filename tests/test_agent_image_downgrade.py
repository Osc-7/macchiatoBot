"""
AgentCore 在主模型无 vision 但有 vision_provider 时的降级行为测试：
- 本轮用户 content_items 里的 image_url 被折叠为文字占位
- 原始媒体登记到 _last_unseen_media，供 recognize_image 按 name 回查
- recognize_image 作为 pinned 工具出现在工作集中
- 待挂载媒体同样被折叠而不是作为多模态消息挂载
"""

from __future__ import annotations

import pytest

from agent_core.agent.agent import AgentCore
from agent_core.agent.media_helpers import (
    adapt_content_items_for_provider,
    append_pending_multimodal_messages,
    downgrade_user_media_to_text,
)
from agent_core.config import (
    AgentConfig,
    CapabilitiesModel,
    Config,
    LLMConfig,
    ProviderEntry,
)


def _make_config(*, vision_provider: str | None = "qwen3vl") -> Config:
    providers = {
        "deepseek": ProviderEntry(
            base_url="https://example.com/v1",
            api_key="k",
            model="deepseek-chat",
            capabilities=CapabilitiesModel(vision=False),
        ),
    }
    if vision_provider:
        providers[vision_provider] = ProviderEntry(
            base_url="https://example.com/v1",
            api_key="k",
            model="qwen-vl",
            capabilities=CapabilitiesModel(vision=True),
        )
    return Config(
        llm=LLMConfig(
            api_key="legacy",
            model="legacy",
            providers=providers,
            active="deepseek",
            vision_provider=vision_provider,
        ),
        agent=AgentConfig(max_iterations=2, enable_debug=False),
    )


def test_downgrade_user_media_registers_unseen_and_returns_placeholder():
    unseen: list = []
    placeholder, kept = downgrade_user_media_to_text(
        [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            {"type": "text", "text": "hello"},
        ],
        unseen_media=unseen,
    )
    assert "[用户附上图片" in placeholder
    assert "recognize_image" in placeholder
    assert kept == [{"type": "text", "text": "hello"}]
    assert len(unseen) == 1
    assert unseen[0]["name"] == "image_1"
    assert unseen[0]["url"].startswith("data:image/")


def test_adapt_content_items_for_provider_keeps_pdf_when_supported():
    preface, adapted = adapt_content_items_for_provider(
        [
            {
                "type": "user_file",
                "name": "spec.pdf",
                "path": "/tmp/spec.pdf",
                "mime_type": "application/pdf",
                "file_data": "JVBERi0xLjc=",
            }
        ],
        supported_file_mime_types=["application/pdf"],
        enable_native_file_blocks=True,
    )
    assert "/tmp/spec.pdf" in preface
    assert "application/pdf" in preface
    assert adapted[0]["type"] == "file"
    assert adapted[0]["file"]["filename"] == "spec.pdf"


def test_adapt_content_items_for_provider_falls_back_to_preview_when_unsupported():
    preface, adapted = adapt_content_items_for_provider(
        [
            {
                "type": "user_file",
                "name": "spec.pdf",
                "path": "/tmp/spec.pdf",
                "mime_type": "application/pdf",
                "file_data": "JVBERi0xLjc=",
                "preview_text": "first page text",
            }
        ],
        supported_file_mime_types=[],
        enable_native_file_blocks=False,
    )
    assert "first page text" in preface
    assert adapted == []


def test_agent_downgrade_registers_recognize_image_tool_when_vision_provider_set():
    agent = AgentCore(config=_make_config(vision_provider="qwen3vl"))
    assert agent.tool_registry.has("recognize_image")
    # 主模型无 vision，vision_provider 存在 -> 工具应该 pinned
    assert "recognize_image" in agent._working_set.pinned_tools


def test_agent_does_not_register_recognize_image_without_vision_provider():
    agent = AgentCore(config=_make_config(vision_provider=None))
    assert not agent.tool_registry.has("recognize_image")


@pytest.mark.asyncio
async def test_prepare_turn_downgrades_image_when_main_lacks_vision():
    agent = AgentCore(config=_make_config(vision_provider="qwen3vl"))
    content_items = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,ZZZ"}},
    ]
    await agent.prepare_turn("请看图", content_items=content_items)

    # 最后一条 user 消息的 content 应该是纯文本（包含占位说明）
    last = agent.context.messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], str)
    assert "[用户附上图片" in last["content"]
    # 原始媒体应被登记到 _last_unseen_media
    assert len(agent._last_unseen_media) == 1
    assert agent._last_unseen_media[0]["url"].startswith("data:image/")


@pytest.mark.asyncio
async def test_prepare_turn_keeps_pdf_file_block_when_provider_supports_it():
    agent = AgentCore(
        config=Config(
            llm=LLMConfig(
                api_key="legacy",
                model="legacy",
                providers={
                    "kimi_like": ProviderEntry(
                        base_url="https://api.kimi.com/coding/v1",
                        api_key="k",
                        model="kimi-for-coding",
                        protocol="anthropic",
                        capabilities=CapabilitiesModel(
                            vision=True,
                            file_input_mime_types=["application/pdf"],
                        ),
                    )
                },
                active="kimi_like",
            ),
            agent=AgentConfig(max_iterations=2, enable_debug=False),
        )
    )
    await agent.prepare_turn(
        "看一下这个文件",
        content_items=[
            {
                "type": "user_file",
                "name": "spec.pdf",
                "path": "/tmp/spec.pdf",
                "mime_type": "application/pdf",
                "file_data": "JVBERi0xLjc=",
            }
        ],
    )
    last = agent.context.messages[-1]
    assert isinstance(last["content"], list)
    assert last["content"][0]["type"] == "text"
    assert last["content"][1]["type"] == "file"


@pytest.mark.asyncio
async def test_prepare_turn_falls_back_to_text_when_file_not_supported():
    agent = AgentCore(config=_make_config(vision_provider="qwen3vl"))
    await agent.prepare_turn(
        "看一下这个文件",
        content_items=[
            {
                "type": "user_file",
                "name": "spec.pdf",
                "path": "/tmp/spec.pdf",
                "mime_type": "application/pdf",
                "file_data": "JVBERi0xLjc=",
                "preview_text": "first page text",
            }
        ],
    )
    last = agent.context.messages[-1]
    assert isinstance(last["content"], str)
    assert "/tmp/spec.pdf" in last["content"]
    assert "first page text" in last["content"]


@pytest.mark.asyncio
async def test_prepare_turn_defers_marked_image_until_next_text_input():
    agent = AgentCore(config=_make_config(vision_provider="qwen3vl"))
    await agent.prepare_turn(
        "",
        content_items=[
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
                "path": "/tmp/p1.png",
                "name": "p1.png",
                "defer_with_next_user_input": True,
            }
        ],
    )
    first = agent.context.messages[-1]
    assert first["role"] == "user"
    assert first["content"] == ""

    await agent.prepare_turn("请根据我刚才发的图回答")
    second = agent.context.messages[-1]
    assert isinstance(second["content"], str)
    assert "请根据我刚才发的图回答" in second["content"]
    assert "p1.png" in second["content"]


def test_append_pending_multimodal_downgrades_without_vision():
    unseen: list = []
    pending = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,YYY"}},
    ]
    msgs = append_pending_multimodal_messages(
        [{"role": "user", "content": "hi"}],
        pending,
        vision_supported=False,
        unseen_media=unseen,
    )
    assert len(msgs) == 2
    extra = msgs[-1]
    assert extra["role"] == "user"
    # vision_supported=False 时拼成纯文本
    assert isinstance(extra["content"], str)
    assert "[用户附上图片" in extra["content"]
    assert len(unseen) == 1


def test_append_pending_multimodal_keeps_list_when_vision_supported():
    pending = [
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
    ]
    msgs = append_pending_multimodal_messages(
        [{"role": "user", "content": "hi"}],
        pending,
        vision_supported=True,
    )
    extra = msgs[-1]
    assert isinstance(extra["content"], list)
    assert extra["content"][1]["type"] == "image_url"


def test_switch_model_updates_recognize_image_visibility():
    agent = AgentCore(config=_make_config(vision_provider="qwen3vl"))
    # 初始 deepseek 无 vision，工具 pinned
    assert "recognize_image" in agent._working_set.pinned_tools
    # 切到 qwen3vl 后主模型具备 vision，工具不再 pinned
    agent.switch_model("qwen3vl")
    assert "recognize_image" not in agent._working_set.pinned_tools
    # 切回 deepseek，工具再次 pinned
    agent.switch_model("deepseek")
    assert "recognize_image" in agent._working_set.pinned_tools
