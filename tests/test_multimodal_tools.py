"""
多模态识图工具测试。
"""

from types import SimpleNamespace

import pytest

from schedule_agent.config import Config, LLMConfig, MultimodalConfig
from schedule_agent.core.tools.multimodal_tools import AnalyzeImageTool


class DummyLLMClient:
    """测试用 LLM client stub。"""

    def __init__(self, content: str = "识图结果"):
        self.content = content
        self.calls = []

    async def chat_with_image(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=self.content)


@pytest.fixture
def base_config():
    return Config(
        llm=LLMConfig(
            provider="qwen",
            api_key="test",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="qwen3.5-plus",
        ),
        multimodal=MultimodalConfig(
            enabled=True,
            model="qwen-vl-max-latest",
            max_image_size_mb=1.0,
        ),
    )


class TestAnalyzeImageTool:
    @pytest.mark.asyncio
    async def test_execute_requires_exactly_one_image_input(self, base_config):
        tool = AnalyzeImageTool(config=base_config, llm_client=DummyLLMClient())
        result = await tool.execute(prompt="test")
        assert result.success is False
        assert result.error == "INVALID_IMAGE_INPUT"

    @pytest.mark.asyncio
    async def test_execute_invalid_image_url(self, base_config):
        tool = AnalyzeImageTool(config=base_config, llm_client=DummyLLMClient())
        result = await tool.execute(image_url="ftp://example.com/a.png")
        assert result.success is False
        assert result.error == "INVALID_IMAGE_URL"

    @pytest.mark.asyncio
    async def test_execute_local_file_not_exists(self, base_config):
        tool = AnalyzeImageTool(config=base_config, llm_client=DummyLLMClient())
        result = await tool.execute(image_path="./not_exists.png")
        assert result.success is False
        assert result.error == "INVALID_IMAGE_PATH"

    @pytest.mark.asyncio
    async def test_execute_local_file_success(self, base_config, tmp_path):
        img_file = tmp_path / "a.png"
        img_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

        llm = DummyLLMClient(content="检测到标题和一段错误码")
        tool = AnalyzeImageTool(config=base_config, llm_client=llm)
        result = await tool.execute(
            image_path=str(img_file),
            prompt="提取文字",
            detail_level="brief",
        )

        assert result.success is True
        assert result.data["analysis"] == "检测到标题和一段错误码"
        assert result.data["source"] == "local_file"
        assert len(llm.calls) == 1
        call = llm.calls[0]
        assert call["model_override"] == "qwen-vl-max-latest"
        assert call["image_url"].startswith("data:image/png;base64,")
