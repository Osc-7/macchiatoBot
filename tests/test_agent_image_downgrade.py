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
