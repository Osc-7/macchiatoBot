"""
媒体挂载工具与回复附图工具。

- AttachMediaTool：将本地图片/视频标记为下一轮对话的多模态**输入**（供 LLM 理解）。
- AttachImageToReplyTool：将图片登记为本轮回复的**输出**附件，用户会在对话中收到该图片（如飞书会收到图片消息）。
"""

from __future__ import annotations

import base64
from pathlib import Path
from dataclasses import dataclass
from typing import Any, List, Optional

from agent_core.config import Config, get_config
from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult


@dataclass
class _AttachMediaParams:
    paths: List[str]

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> _AttachMediaParams:
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


def _remote_workspace_active(exec_ctx: dict) -> bool:
    sid = str((exec_ctx or {}).get("session_id") or "").strip()
    if not sid:
        return False
    try:
        from agent_core.remote.workspace_state import get_remote_workspace_state

        return get_remote_workspace_state(sid) is not None
    except Exception:
        return False


async def _read_remote_attachment_blob(
    *,
    path_str: str,
    exec_ctx: dict,
    max_bytes: int,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    sid = str((exec_ctx or {}).get("session_id") or "").strip()
    if not sid:
        return None, "缺少远程会话 session_id"
    try:
        from agent_core.remote.pathmap import normalize_remote_workspace_relative_path
        from agent_core.remote.worker_registry import get_remote_worker_registry
        from agent_core.remote.workspace_state import get_remote_workspace_state

        state = get_remote_workspace_state(sid)
        if state is None:
            return None, "远程会话未激活"
        rel, verr = normalize_remote_workspace_relative_path(path_str)
        if verr or rel is None:
            return None, verr or "无效远程路径"
        blob = await get_remote_worker_registry().file_blob_read(
            login=state.login,
            session_id=sid,
            path=rel,
            max_bytes=max_bytes,
        )
    except Exception as exc:
        exc_name = exc.__class__.__name__
        msg = str(exc).strip()
        if isinstance(exc, TimeoutError):
            return None, "远程读取附件超时（等待 remote file_blob_read 响应超过 120s）"
        if msg:
            return None, f"远程读取附件失败: {exc_name}: {msg}"
        return None, f"远程读取附件失败: {exc_name}"
    if blob.error:
        return None, blob.error
    if not blob.content_base64:
        return None, "远程附件为空"
    return (
        {
            "content_base64": blob.content_base64,
            "file_name": blob.file_name,
            "mime_type": blob.mime_type,
            "bytes_read": blob.bytes_read,
            "truncated": blob.truncated,
        },
        None,
    )


async def _read_remote_attachment_text_fallback(
    *,
    path_str: str,
    exec_ctx: dict,
    max_bytes: int,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """兼容旧远程 worker：当 file_blob_read 不可用时，降级为 UTF-8 文本读取。"""
    sid = str((exec_ctx or {}).get("session_id") or "").strip()
    if not sid:
        return None, "缺少远程会话 session_id"
    try:
        from agent_core.remote.pathmap import normalize_remote_workspace_relative_path
        from agent_core.remote.worker_registry import get_remote_worker_registry
        from agent_core.remote.workspace_state import get_remote_workspace_state

        state = get_remote_workspace_state(sid)
        if state is None:
            return None, "远程会话未激活"
        rel, verr = normalize_remote_workspace_relative_path(path_str)
        if verr or rel is None:
            return None, verr or "无效远程路径"
        text_result = await get_remote_worker_registry().file_read(
            login=state.login,
            session_id=sid,
            path=rel,
            encoding="utf-8",
            timeout_seconds=30.0,
        )
    except Exception as exc:
        exc_name = exc.__class__.__name__
        msg = str(exc).strip()
        if isinstance(exc, TimeoutError):
            return None, "远程文本兜底读取也超时（worker 可能未响应）"
        if msg:
            return None, f"远程文本兜底读取失败: {exc_name}: {msg}"
        return None, f"远程文本兜底读取失败: {exc_name}"

    if text_result.error:
        return None, text_result.error
    content_bytes = str(text_result.content or "").encode("utf-8", errors="replace")
    truncated = bool(text_result.truncated)
    if len(content_bytes) > max_bytes:
        content_bytes = content_bytes[: max(1, int(max_bytes))]
        truncated = True
    return (
        {
            "content_base64": base64.b64encode(content_bytes).decode("ascii"),
            "file_name": Path(path_str).name or "attachment.txt",
            "mime_type": "text/plain; charset=utf-8",
            "bytes_read": len(content_bytes),
            "truncated": truncated,
        },
        None,
    )


def _resolve_reply_attachment_path(
    path_str: str,
    *,
    config: Config,
    exec_ctx: dict,
) -> tuple[Optional[Path], Optional[str]]:
    from agent_core.agent.tool_path_resolution import resolve_path_string_for_tool

    resolved, err = resolve_path_string_for_tool(path_str, config, exec_ctx)
    if err or resolved is None:
        return None, err or f"无法解析路径: {path_str}"
    return resolved, None


class AttachMediaTool(BaseTool):
    """
    将本地图片/视频挂载到下一轮 LLM 调用的多模态消息中。

    注意：本工具**不直接进行识图/视频理解**，而是声明「下一轮请求需要附带这些媒体」。
    实际的多模态理解发生在下一次 chat_with_tools 调用中，由当前主模型统一处理文字+图像/视频。
    """

    def __init__(self, config: Optional[Config] = None) -> None:
        self._config = config or get_config()

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
- AgentCore 的运行时会在**下一轮 LLM 调用前**自动把这些媒体嵌入到 messages 里

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
                    items={"type": "string"},
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

        ctx = kwargs.get("__execution_context__") or {}
        unique_paths = list(dict.fromkeys(params.paths))
        media_items: List[Dict[str, Any]] = []
        errors: List[str] = []

        from agent_core.utils.media import resolve_media_to_content_item

        for raw_path in unique_paths:
            item, err = resolve_media_to_content_item(
                raw_path, config=self._config, exec_ctx=ctx
            )
            if err or item is None:
                errors.append(f"{raw_path}: {err or '无法解析媒体'}")
                continue
            media_type = str(item.get("media_type") or "").strip().lower()
            if media_type == "video":
                errors.append(
                    f"{raw_path}: 当前默认不把视频直接挂载给模型，请改用文字描述或截图。"
                )
                continue
            if item.get("type") == "media_ref" and media_type == "image":
                media_items.append(item)
            else:
                errors.append(f"{raw_path}: 仅支持挂载图片（image）")

        if not media_items:
            return ToolResult(
                success=False,
                error="INVALID_MEDIA_PATH",
                message="；".join(errors) if errors else "没有可挂载的图片。",
            )

        msg = "图片已标记，将在下一轮 LLM 调用中附加（Kimi 等 provider 会优先走 Files API ms:// 引用）。"
        if errors:
            msg += f" 部分路径已跳过：{'；'.join(errors)}"

        return ToolResult(
            success=True,
            data={"paths": [str(i.get("path") or "") for i in media_items if i.get("path")]},
            message=msg,
            metadata={
                "embed_in_next_call": True,
                "paths": [str(i.get("path") or "") for i in media_items if i.get("path")],
                "media_items": media_items,
            },
        )


class AttachImageToReplyTool(BaseTool):
    """
    将一张图片登记为「随本轮回复一起发给用户」的附件。

    调用后，图片会随 Agent 的文本回复一并发送到当前会话（如飞书会收到一条图片消息）。
    """

    def __init__(self, config: Optional[Config] = None) -> None:
        self._config = config or get_config()

    @property
    def name(self) -> str:
        return "attach_image_to_reply"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="attach_image_to_reply",
            description="""将一张图片随本轮回复一起发给用户。

当你要**向用户展示**某张图片时（例如截图、示意图、错误界面截图），使用本工具：
- 提供本地图片路径（如 bash 命令或浏览器自动化保存的截图路径），或一张图片的 URL
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
                {
                    "description": "把刚截的登录页截图发给用户看",
                    "params": {"image_path": "pictures/canvas_login.png"},
                },
                {
                    "description": "把网络图片登记为回复附图",
                    "params": {"image_url": "https://example.com/diagram.png"},
                },
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
            ctx = kwargs.get("__execution_context__") or {}
            if _remote_workspace_active(ctx):
                blob, berr = await _read_remote_attachment_blob(
                    path_str=str(image_path).strip(),
                    exec_ctx=ctx,
                    max_bytes=10 * 1024 * 1024,
                )
                if berr or blob is None:
                    return ToolResult(
                        success=False,
                        error="REMOTE_ATTACHMENT_READ_FAILED",
                        message=berr or f"无法读取远程图片: {image_path}",
                    )
                attachment = {
                    "type": "image",
                    "content_base64": blob["content_base64"],
                    "content_type": blob["mime_type"],
                }
                if blob.get("file_name"):
                    attachment["file_name"] = blob["file_name"]
                return ToolResult(
                    success=True,
                    data=attachment,
                    message="图片已加入回复附件，用户将在对话中看到该图片。",
                    metadata={"outgoing_attachment": attachment},
                )
            p, err = _resolve_reply_attachment_path(
                str(image_path).strip(),
                config=self._config,
                exec_ctx=ctx,
            )
            if err or p is None:
                return ToolResult(
                    success=False,
                    error="INVALID_PATH",
                    message=err or f"无法解析图片路径: {image_path}",
                )
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


class AttachFileToReplyTool(BaseTool):
    """将一个文件登记为「随本轮回复一起发给用户」的附件。"""

    def __init__(self, config: Optional[Config] = None) -> None:
        self._config = config or get_config()

    @property
    def name(self) -> str:
        return "attach_file_to_reply"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="attach_file_to_reply",
            description="""将一个文件随本轮回复一起发给用户。

当用户明确要“发文件/把文件给我”时，使用本工具：
- 提供本地文件路径（推荐）或 http(s) 下载地址
- 工具会将文件登记为本轮回复附件；前端（如飞书）会上传并发送文件消息
""",
            parameters=[
                ToolParameter(
                    name="file_path",
                    type="string",
                    description="本地文件路径（与 file_url 二选一）",
                    required=False,
                ),
                ToolParameter(
                    name="file_url",
                    type="string",
                    description="文件下载 URL（http/https，与 file_path 二选一）",
                    required=False,
                ),
                ToolParameter(
                    name="file_name",
                    type="string",
                    description="可选：发送时使用的文件名；未提供则沿用路径名/URL 推断名",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "把本地日志文件发给用户",
                    "params": {"file_path": "reports/debug.log"},
                },
                {
                    "description": "把远程文档发给用户",
                    "params": {
                        "file_url": "https://example.com/spec.pdf",
                        "file_name": "spec.pdf",
                    },
                },
            ],
            usage_notes=[
                "file_path 与 file_url 必须且只能提供一个。",
                "建议优先使用 file_path，避免远程下载失败导致发送失败。",
            ],
            tags=["附件", "回复", "文件", "飞书"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        file_path = kwargs.get("file_path")
        file_url = kwargs.get("file_url")
        file_name = str(kwargs.get("file_name") or "").strip()

        if bool(file_path) == bool(file_url):
            return ToolResult(
                success=False,
                error="INVALID_INPUT",
                message="必须且只能提供 file_path 或 file_url 其中一个",
            )

        if file_path:
            ctx = kwargs.get("__execution_context__") or {}
            if _remote_workspace_active(ctx):
                blob, berr = await _read_remote_attachment_blob(
                    path_str=str(file_path).strip(),
                    exec_ctx=ctx,
                    max_bytes=50 * 1024 * 1024,
                )
                if berr or blob is None:
                    fallback_blob, fallback_err = (
                        await _read_remote_attachment_text_fallback(
                            path_str=str(file_path).strip(),
                            exec_ctx=ctx,
                            max_bytes=50 * 1024 * 1024,
                        )
                    )
                    if fallback_blob is not None:
                        blob = fallback_blob
                    else:
                        return ToolResult(
                            success=False,
                            error="REMOTE_ATTACHMENT_READ_FAILED",
                            message=(
                                fallback_err
                                or berr
                                or f"无法读取远程文件: {file_path}"
                            ),
                        )
                attachment = {
                    "type": "file",
                    "content_base64": blob["content_base64"],
                    "mime_type": blob["mime_type"],
                    "file_name": file_name or blob.get("file_name") or "attachment.bin",
                }
                return ToolResult(
                    success=True,
                    data=attachment,
                    message="文件已加入回复附件，用户将在对话中收到该文件。",
                    metadata={"outgoing_attachment": attachment},
                )
            p, err = _resolve_reply_attachment_path(
                str(file_path).strip(),
                config=self._config,
                exec_ctx=ctx,
            )
            if err or p is None:
                return ToolResult(
                    success=False,
                    error="INVALID_PATH",
                    message=err or f"无法解析文件路径: {file_path}",
                )
            if not p.exists() or not p.is_file():
                return ToolResult(
                    success=False,
                    error="FILE_NOT_FOUND",
                    message=f"文件不存在或不是文件: {p}",
                )
            attachment = {"type": "file", "path": str(p)}
            if file_name:
                attachment["file_name"] = file_name
        else:
            url_str = str(file_url).strip()
            if not url_str.startswith(("http://", "https://")):
                return ToolResult(
                    success=False,
                    error="INVALID_URL",
                    message="file_url 必须以 http:// 或 https:// 开头",
                )
            attachment = {"type": "file", "url": url_str}
            if file_name:
                attachment["file_name"] = file_name

        return ToolResult(
            success=True,
            data=attachment,
            message="文件已加入回复附件，用户将在对话中收到该文件。",
            metadata={"outgoing_attachment": attachment},
        )
