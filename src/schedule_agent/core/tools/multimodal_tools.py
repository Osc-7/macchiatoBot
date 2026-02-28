"""
多模态工具：基于多模态大模型进行识图分析。
"""

import base64
import asyncio
import mimetypes
from pathlib import Path
from typing import Optional

from schedule_agent.config import Config, get_config
from schedule_agent.core.llm import LLMClient

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult


class AnalyzeImageTool(BaseTool):
    """调用多模态大模型分析图片内容。"""

    def __init__(
        self,
        config: Optional[Config] = None,
        llm_client: Optional[LLMClient] = None,
    ):
        self._config = config or get_config()
        self._llm_client = llm_client or LLMClient(self._config)

    @property
    def name(self) -> str:
        return "analyze_image"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="analyze_image",
            description="""使用多模态大模型分析图片内容。

当用户想要：
- 识别截图或照片中的文字
- 理解图片里发生了什么
- 提取表格、票据、界面中的关键信息

工具会自动：
- 接收本地图片路径或网络图片 URL
- 调用多模态模型进行视觉理解
- 返回结构化分析结果""",
            parameters=[
                ToolParameter(
                    name="image_path",
                    type="string",
                    description="本地图片路径（与 image_url 二选一）",
                    required=False,
                ),
                ToolParameter(
                    name="image_url",
                    type="string",
                    description="网络图片 URL（http:// 或 https://，与 image_path 二选一）",
                    required=False,
                ),
                ToolParameter(
                    name="prompt",
                    type="string",
                    description="分析要求，例如：提取所有文字、总结主要信息、定位错误提示等",
                    required=False,
                ),
                ToolParameter(
                    name="detail_level",
                    type="string",
                    description="输出详细程度",
                    required=False,
                    enum=["brief", "normal", "detailed"],
                ),
            ],
            examples=[
                {
                    "description": "识别本地截图文字",
                    "params": {
                        "image_path": "./screenshots/error.png",
                        "prompt": "提取报错信息并给出可能原因",
                    },
                },
                {
                    "description": "分析在线图片",
                    "params": {
                        "image_url": "https://example.com/receipt.jpg",
                        "prompt": "提取金额、时间、商家名称",
                    },
                },
            ],
            usage_notes=[
                "image_path 和 image_url 必须且只能提供一个",
                "建议在 prompt 中明确提取目标，结果会更稳定",
                "本工具走多模态 LLM，不依赖本地 OCR 模型推理速度",
            ],
            tags=["多模态", "识图", "OCR"],
        )

    @staticmethod
    def _build_prompt(user_prompt: Optional[str], detail_level: str) -> str:
        base_prompt = (user_prompt or "请识别图片中的文字并概括主要内容。").strip()
        detail_map = {
            "brief": "请用简洁要点输出。",
            "normal": "请输出清晰分段结果，包含关键细节。",
            "detailed": "请尽可能完整提取信息，并标注不确定内容。",
        }
        suffix = detail_map.get(detail_level, detail_map["normal"])
        return f"{base_prompt}\n\n{suffix}"

    def _local_image_to_data_url(self, image_path: str) -> tuple[Optional[str], Optional[str]]:
        p = Path(image_path).expanduser().resolve()
        if not p.exists() or not p.is_file():
            return None, f"图片不存在: {p}"

        max_bytes = int(self._config.multimodal.max_image_size_mb * 1024 * 1024)
        file_size = p.stat().st_size
        if file_size > max_bytes:
            return (
                None,
                f"图片过大: {file_size} bytes，超过限制 {max_bytes} bytes",
            )

        mime, _ = mimetypes.guess_type(str(p))
        if not mime:
            mime = "application/octet-stream"

        encoded = base64.b64encode(p.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}", None

    async def execute(self, **kwargs) -> ToolResult:
        image_path = kwargs.get("image_path")
        image_url = kwargs.get("image_url")
        prompt = kwargs.get("prompt")
        detail_level = kwargs.get("detail_level", "normal")

        if bool(image_path) == bool(image_url):
            return ToolResult(
                success=False,
                error="INVALID_IMAGE_INPUT",
                message="必须且只能提供 image_path 或 image_url 其中一个",
            )

        final_image_url: Optional[str] = None
        image_source = "remote_url"
        if image_path:
            final_image_url, err = self._local_image_to_data_url(str(image_path))
            if err:
                return ToolResult(
                    success=False,
                    error="INVALID_IMAGE_PATH",
                    message=err,
                )
            image_source = "local_file"
        else:
            image_url_str = str(image_url).strip()
            if not image_url_str.startswith(("http://", "https://")):
                return ToolResult(
                    success=False,
                    error="INVALID_IMAGE_URL",
                    message="image_url 必须以 http:// 或 https:// 开头",
                )
            final_image_url = image_url_str

        req_prompt = self._build_prompt(prompt, detail_level)
        try:
            response = await asyncio.wait_for(
                self._llm_client.chat_with_image(
                    prompt=req_prompt,
                    image_url=final_image_url or "",
                    system_message="你是一个严谨的图像理解助手，请优先提取事实信息，不要臆测。",
                    model_override=self._config.multimodal.model,
                ),
                timeout=self._config.multimodal.request_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error="MULTIMODAL_TIMEOUT",
                message="识图请求超时，请缩小图片或稍后重试",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error="MULTIMODAL_REQUEST_FAILED",
                message=f"识图请求失败: {str(e)}",
            )

        if not response.content:
            return ToolResult(
                success=False,
                error="EMPTY_MULTIMODAL_RESPONSE",
                message="识图请求成功但未返回有效内容",
            )

        return ToolResult(
            success=True,
            data={
                "analysis": response.content,
                "source": image_source,
                "model": self._config.multimodal.model or self._config.llm.model,
            },
            message="识图完成",
        )
