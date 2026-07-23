"""
多模态媒体辅助函数。

用于将本地图片/视频文件转换为可注入 OpenAI 兼容 messages 的 content item。
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

if TYPE_CHECKING:
    from agent_core.config import Config


def _project_root() -> Path:
    # /work/src/agent/utils/media.py -> /work
    return Path(__file__).resolve().parents[3]


def _resolve_media_path(
    media_path: str,
    *,
    config: Optional["Config"] = None,
    exec_ctx: Optional[dict] = None,
) -> Path:
    """
    将媒体路径解析为绝对路径。

    解析策略：
    1) ``~`` / ``~/``：若传入 ``config``，与会话工作区对齐（见 ``session_paths``）
    2) 绝对路径：直接使用
    3) 相对路径：与 file_tools 一致，优先相对当前会话工作区（隔离开启时）或
       file_tools.base_dir；若不存在再尝试项目根与 ``user_file/``（兼容旧行为）
    """
    raw = (media_path or "").strip()
    if config is not None:
        from agent_core.agent.session_paths import expand_user_path_str_for_session

        raw = expand_user_path_str_for_session(raw, config, exec_ctx=exec_ctx or {})
    else:
        raw = str(Path(raw).expanduser())
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()

    root = _project_root()

    if config is not None:
        from agent_core.agent.tool_path_resolution import resolve_path_string_for_tool

        session_path, _err = resolve_path_string_for_tool(
            raw, config, exec_ctx or {}
        )
        if session_path is not None and session_path.exists():
            return session_path.resolve()

    direct = (root / p).resolve()
    if direct.exists():
        return direct

    return (root / "user_file" / p).resolve()


def _remote_workspace_active(exec_ctx: Optional[dict]) -> bool:
    sid = str((exec_ctx or {}).get("session_id") or "").strip()
    if not sid:
        return False
    try:
        from agent_core.remote.workspace_state import get_remote_workspace_state

        return get_remote_workspace_state(sid) is not None
    except Exception:
        return False


def _file_to_data_url(path: Path) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    将文件编码为 data URL。

    Returns:
        (data_url, mime, error)
    """
    if not path.exists() or not path.is_file():
        return None, None, f"媒体文件不存在: {path}"

    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        mime = "application/octet-stream"

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}", mime, None


def resolve_media_to_content_item(
    media_path: str,
    *,
    config: Optional["Config"] = None,
    exec_ctx: Optional[dict] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    将媒体路径转换为多模态 content item（image_url / video_url）。

    传入 ``config`` / ``exec_ctx`` 时，``~`` 与会话工作区一致（与 attach_media 路径对齐）。

    Returns:
        (content_item, error)
    """
    path = _resolve_media_path(media_path, config=config, exec_ctx=exec_ctx)
    if config is not None and _remote_workspace_active(exec_ctx):
        if not (path.exists() and path.is_file()):
            return (
                None,
                "远程工作区下暂不支持直接挂载远程媒体路径；请提供 http(s)/data URL，或先把媒体同步到 daemon 可读路径。",
            )
    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        mime = "application/octet-stream"

    if (mime or "").startswith("video/"):
        return {
            "type": "media_ref",
            "media_type": "video",
            "path": str(path),
            "name": path.name,
            "mime_type": mime,
        }, None

    return {
        "type": "media_ref",
        "media_type": "image",
        "path": str(path),
        "name": path.name,
        "mime_type": mime,
    }, None


def resolve_media_path_to_data_url(
    media_path: str,
    *,
    config: Optional["Config"] = None,
    exec_ctx: Optional[dict] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a local media path to a data URL for vision fallback tools."""
    content_item, err = resolve_media_to_content_item(
        media_path, config=config, exec_ctx=exec_ctx
    )
    if err or not content_item:
        return None, err or f"无法解析媒体路径: {media_path}"
    media_type = str(content_item.get("media_type") or "").strip().lower()
    if media_type and media_type != "image":
        return None, f"路径指向的不是图像（media_type={media_type}）"
    path_str = str(content_item.get("path") or "").strip()
    if not path_str:
        return None, f"无法解析媒体路径: {media_path}"
    data_url, _mime, data_err = _file_to_data_url(Path(path_str))
    if data_err or not data_url:
        return None, data_err or f"无法读取媒体文件: {media_path}"
    return data_url, None
