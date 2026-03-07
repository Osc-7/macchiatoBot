"""
媒体挂载工具与回复附图工具。

- AttachMediaTool：将本地图片/视频标记为下一轮对话的多模态**输入**（供 LLM 理解）。
- AttachImageToReplyTool：将图片登记为本轮回复的**输出**附件，用户会在对话中收到该图片（如飞书会收到图片消息）。
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult


@dataclass
class _AttachMediaParams:
    paths: List[str]

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> " _AttachMediaParams":
        path = kwargs.get("path")
        paths = kwargs.get("paths")

        collected: List[str] = []
        if isinstance(path, str) and path.strip():
            collected.append(path.strip())
        if isinstance(paths, list):
            for item in paths:
                if isinstance(item, str) and item.strip():
                    collected.append(item.strip())

        return cls(paths=collected)


class AttachMediaTool(BaseTool):
    """
    将本地图片/视频挂载到下一轮 LLM 调用的多模态消息中。

    注意：本工具**不直接进行识图/视频理解**，而是声明「下一轮请求需要附带这些媒体」。
    实际的多模态理解发生在下一次 chat_with_tools 调用中，由当前主模型统一处理文字+图像/视频。
    """

    @property
    def name(self) -> str:
        return "attach_media"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="attach_media",
            description="""将本地图片/视频挂载为下一轮对话的多模态输入。

当你在推理中发现「需要查看某张截图/某个视频片段」时，使用本工具：
- 提供位于工作区（通常是 user_file/ 目录）中的媒体路径
- 工具不会直接调用多模态模型，只会在 metadata 中声明挂载请求
- ScheduleAgent 的运行时会在**下一轮 LLM 调用前**自动把这些媒体嵌入到 messages 里

推荐用法：
- 用户或其他工具先将文件保存到 user_file/ 目录
- 你调用 attach_media(path=\"user_file/xxx.png\") 或 attach_media(paths=[...])
- 下一轮回答时，直接根据「刚刚挂载的截图」继续推理，无需再关心 base64 或 URL 细节。

**注意**：若用户说「把图发给我」「发图给用户看」「试一下发图」，应使用 attach_image_to_reply（发给用户），不要用本工具（本工具只是把图挂载给你自己下一轮分析，用户收不到）。
""",
            parameters=[
                ToolParameter(
                    name="path",
                    type="string",
                    description="单个媒体路径（优先使用相对 user_file/ 的路径，也可为绝对路径）",
                    required=False,
                ),
                ToolParameter(
                    name="paths",
                    type="array",
                    description="多个媒体路径列表，与 path 二选一；两者同时提供时会合并去重。",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "挂载一张错误截图，供下一轮分析",
                    "params": {"path": "user_file/error_screenshot.png"},
                },
                {
                    "description": "一次挂载多页 PDF 截图",
                    "params": {
                        "paths": [
                            "user_file/page_1.png",
                            "user_file/page_2.png",
                        ]
                    },
                },
            ],
            usage_notes=[
                "本工具不会直接返回图片内容或进行识图，只是声明下一轮需要附带的媒体。",
                "路径推荐使用 user_file/ 前缀下的相对路径，方便与上传逻辑对齐。",
                "调用成功后，你可以在后续回复中自然地引用这些媒体，例如：“根据刚才挂载的截图……”。",
            ],
            tags=["多模态", "媒体", "挂载"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        params = _AttachMediaParams.from_kwargs(**kwargs)
        if not params.paths:
            return ToolResult(
                success=False,
                error="MISSING_MEDIA_PATH",
                message="必须提供 path 或 paths 中的至少一个媒体路径。",
            )

        # 不做实际文件读取，仅把路径交给 runtime，在下一轮调用前注入多模态内容。
        unique_paths = list(dict.fromkeys(params.paths))

        return ToolResult(
            success=True,
            data={"paths": unique_paths},
            message="媒体已标记，将在下一轮 LLM 调用中作为多模态输入附加。",
            metadata={"embed_in_next_call": True, "paths": unique_paths},
        )


class AttachImageToReplyTool(BaseTool):
    """
    将一张图片登记为「随本轮回复一起发给用户」的附件。

    调用后，图片会随 Agent 的文本回复一并发送到当前会话（如飞书会收到一条图片消息）。
    """

    @property
    def name(self) -> str:
        return "attach_image_to_reply"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="attach_image_to_reply",
            description="""将一张图片随本轮回复一起发给用户。

当你要**向用户展示**某张图片时（例如截图、示意图、错误界面截图），使用本工具：
- 提供本地图片路径（如 run_command 或浏览器自动化保存的截图路径），或一张图片的 URL
- 工具会将该图片登记为本轮回复的附件；回复发送时用户会在对话中看到这张图（飞书等会收到图片消息）

**重要**：用户说「发给我」「发图给我」「把截图/图发过来」「试一下发图」等，都是要求把图**发给用户看**，必须用本工具 attach_image_to_reply，不要用 attach_media。

与 attach_media 的区别：
- attach_media：把图片挂载为**下一轮你（LLM）的输入**，供你分析，用户看不到
- attach_image_to_reply：把图片**发给用户看**，会随你的文字回复一起出现在对话里
""",
            parameters=[
                ToolParameter(
                    name="image_path",
                    type="string",
                    description="本地图片文件路径（与 image_url 二选一）",
                    required=False,
                ),
                ToolParameter(
                    name="image_url",
                    type="string",
                    description="图片的 http(s) URL（与 image_path 二选一）",
                    required=False,
                ),
            ],
            examples=[
                {"description": "把刚截的登录页截图发给用户看", "params": {"image_path": "pictures/canvas_login.png"}},
                {"description": "把网络图片登记为回复附图", "params": {"image_url": "https://example.com/diagram.png"}},
            ],
            usage_notes=[
                "image_path 与 image_url 必须且只能提供一个。",
                "本地路径会经解析后传给前端；前端（如飞书）会上传该文件并发送图片消息。",
            ],
            tags=["多模态", "回复", "图片", "飞书"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        image_path = kwargs.get("image_path")
        image_url = kwargs.get("image_url")

        if bool(image_path) == bool(image_url):
            return ToolResult(
                success=False,
                error="INVALID_INPUT",
                message="必须且只能提供 image_path 或 image_url 其中一个",
            )

        if image_path:
            path_str = str(image_path).strip()
            p = Path(path_str).expanduser().resolve()
            if not p.exists() or not p.is_file():
                return ToolResult(
                    success=False,
                    error="FILE_NOT_FOUND",
                    message=f"图片文件不存在或不是文件: {p}",
                )
            attachment = {"type": "image", "path": str(p)}
        else:
            url_str = str(image_url).strip()
            if not url_str.startswith(("http://", "https://")):
                return ToolResult(
                    success=False,
                    error="INVALID_URL",
                    message="image_url 必须以 http:// 或 https:// 开头",
                )
            attachment = {"type": "image", "url": url_str}

        return ToolResult(
            success=True,
            data=attachment,
            message="图片已加入回复附件，用户将在对话中看到该图片。",
            metadata={"outgoing_attachment": attachment},
        )
