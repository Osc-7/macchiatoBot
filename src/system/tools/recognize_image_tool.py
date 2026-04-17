"""
识图工具（非 VL 主模型下的识图回落）。

当主对话 LLM 不支持 vision 时，AgentCore 会把本工具暴露给模型。模型可调用本工具
让 vision_provider 描述图片内容，再把文字描述融入推理。

工具行为：
- 接受 image_path（本地相对/绝对，或 user_file/ 下的相对路径）或 image_url(http(s))
- 可选 question，作为 prompt 向 VL 模型提问（缺省时要求描述图片内容）
- 内部调用 LLMClient.chat_with_image(..., provider_name=<vision_provider>)
- 返回 VL 模型的文字描述
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agent_core.config import Config, get_config
from agent_core.llm.client import LLMClient
from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from agent_core.utils.media import resolve_media_to_content_item

logger = logging.getLogger(__name__)


_DEFAULT_QUESTION = (
    "请详细描述这张图片的内容，包括主要元素、文本、布局、颜色等可观察到的信息。"
    "如果图中有文字或代码，请完整转写。"
)


class RecognizeImageTool(BaseTool):
    """
    识别图片内容。

    当主对话模型没有视觉能力时，由 AgentCore 暴露给模型，
    让模型显式调用本工具把图片发给 vision_provider 处理。
    """

    def __init__(
        self,
        *,
        llm_client: LLMClient,
        config: Optional[Config] = None,
        unseen_media: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._llm_client = llm_client
        self._config = config or get_config()
        # 指向 AgentCore._last_unseen_media；本工具可在只拿到 name 时回查路径
        self._unseen_media = unseen_media if unseen_media is not None else []

    @property
    def name(self) -> str:
        return "recognize_image"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="recognize_image",
            description="""识别图片内容并以文字形式返回描述。

**何时使用**：
- 当前主模型不具备视觉能力（即你看不到用户发来的图），需要通过文字描述图片内容时
- 用户消息里看到类似「[用户附上图片 name=... path=...]」的提示，意味着主模型没直接看到该图；
  本工具会把图片送到专门的视觉模型（vision_provider）并返回文字描述

**参数**：
- image_path: 本地路径（绝对或相对 user_file/），或之前登记的 name 字段
- image_url: http(s) 图片 URL；与 image_path 二选一
- question: 可选，向视觉模型的提问；缺省时默认要求完整描述图像

**典型用例**：
- 用户「这图里写了什么？」→ 调 recognize_image(image_path="user_file/error.png", question="这张图里的报错文本是什么？")
- 用户「帮我分析截图」→ 调 recognize_image(image_path=<path>, question="请描述截图中的界面和关键信息")
""",
            parameters=[
                ToolParameter(
                    name="image_path",
                    type="string",
                    description=(
                        "本地图片路径（相对 user_file/ 或绝对路径；也可以是消息里 "
                        "[用户附上图片 name=...] 的 name 值）。与 image_url 二选一。"
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="image_url",
                    type="string",
                    description="图片的 http(s) URL。与 image_path 二选一。",
                    required=False,
                ),
                ToolParameter(
                    name="question",
                    type="string",
                    description="可选，向视觉模型的提问；缺省时要求全面描述。",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "分析用户发来的错误截图",
                    "params": {
                        "image_path": "user_file/error.png",
                        "question": "截图中报了什么错？请把报错信息完整转写。",
                    },
                },
                {
                    "description": "识别网络图片",
                    "params": {
                        "image_url": "https://example.com/chart.png",
                    },
                },
            ],
            usage_notes=[
                "本工具会在内部切到 vision_provider 调用一次多模态请求，额外消耗 token。",
                "如果主模型本身就能看图，不需要使用本工具；AgentCore 不会在这种情况下注册它。",
                "只传 name 时会在 AgentCore 登记的 unseen_media 中回查路径；仍找不到则报错。",
            ],
            tags=["多模态", "识图", "视觉回落"],
        )

    def _lookup_unseen_media(self, key: str) -> Optional[Dict[str, Any]]:
        """在 AgentCore._last_unseen_media 中按 name / path 反查一条媒体记录。"""
        key = (key or "").strip()
        if not key:
            return None
        for item in self._unseen_media:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip() == key:
                return item
            if str(item.get("path") or "").strip() == key:
                return item
        return None

    async def execute(self, **kwargs: Any) -> ToolResult:
        image_path = kwargs.get("image_path")
        image_url = kwargs.get("image_url")
        question = (kwargs.get("question") or _DEFAULT_QUESTION).strip() or _DEFAULT_QUESTION

        if not image_path and not image_url:
            return ToolResult(
                success=False,
                error="MISSING_IMAGE",
                message="必须提供 image_path 或 image_url 其中一个。",
            )
        if image_path and image_url:
            return ToolResult(
                success=False,
                error="CONFLICTING_INPUT",
                message="image_path 与 image_url 只能提供一个。",
            )

        ctx = kwargs.get("__execution_context__") or {}

        resolved_url: Optional[str] = None
        if image_url:
            url = str(image_url).strip()
            if not url.startswith(("http://", "https://", "data:")):
                return ToolResult(
                    success=False,
                    error="INVALID_URL",
                    message="image_url 必须以 http://、https:// 或 data: 开头。",
                )
            resolved_url = url
        else:
            # image_path 可能是 name；先从 unseen_media 反查
            original_key = str(image_path).strip()
            lookup = self._lookup_unseen_media(original_key)
            if lookup:
                # 有登记条目时，优先使用已登记的 url（可能是 data URL），
                # 其次再尝试重新按 path 解析为 data URL。
                registered_url = str(lookup.get("url") or "").strip()
                if registered_url:
                    resolved_url = registered_url
                elif lookup.get("path"):
                    content_item, err = resolve_media_to_content_item(
                        str(lookup.get("path") or "").strip(),
                        config=self._config,
                        exec_ctx=ctx,
                    )
                    if err or not content_item:
                        return ToolResult(
                            success=False,
                            error="RESOLVE_FAILED",
                            message=err or f"无法解析登记的图片: {original_key}",
                        )
                    if content_item.get("type") != "image_url":
                        return ToolResult(
                            success=False,
                            error="NOT_AN_IMAGE",
                            message=f"登记条目不是图像（type={content_item.get('type')}）。",
                        )
                    resolved_url = (content_item.get("image_url") or {}).get("url")
            else:
                content_item, err = resolve_media_to_content_item(
                    original_key, config=self._config, exec_ctx=ctx
                )
                if err or not content_item:
                    return ToolResult(
                        success=False,
                        error="RESOLVE_FAILED",
                        message=err or f"无法解析图片路径: {image_path}",
                    )
                media_type = content_item.get("type")
                if media_type != "image_url":
                    return ToolResult(
                        success=False,
                        error="NOT_AN_IMAGE",
                        message=f"路径指向的不是图像（type={media_type}）；recognize_image 仅支持图像。",
                    )
                resolved_url = (content_item.get("image_url") or {}).get("url")

        if not resolved_url:
            return ToolResult(
                success=False,
                error="RESOLVE_FAILED",
                message="无法得到可用的图片 URL。",
            )

        vision_provider = self._llm_client.vision_provider_name
        if not vision_provider:
            return ToolResult(
                success=False,
                error="NO_VISION_PROVIDER",
                message=(
                    "未配置任何具备 vision 能力的 provider。请在 config.yaml 的 llm.providers "
                    "下声明 capabilities.vision=true，并设置 llm.vision_provider。"
                ),
            )

        try:
            response = await self._llm_client.chat_with_image(
                prompt=question,
                image_url=resolved_url,
                provider_name=vision_provider,
            )
        except Exception as exc:
            logger.exception("recognize_image 调用 vision provider 失败")
            return ToolResult(
                success=False,
                error="VISION_CALL_FAILED",
                message=f"视觉模型调用失败: {exc}",
            )

        description = (response.content or "").strip()
        if not description:
            return ToolResult(
                success=False,
                error="EMPTY_DESCRIPTION",
                message="视觉模型未返回描述内容（可能被上游拒绝或内容被过滤）。",
            )

        return ToolResult(
            success=True,
            data={
                "description": description,
                "vision_provider": vision_provider,
            },
            message=description,
            metadata={"vision_provider": vision_provider},
        )
