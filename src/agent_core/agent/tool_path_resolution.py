"""
工具入参路径解析：将会话视角下的相对路径统一为可访问的绝对路径。

与 file_tools 中「读路径」语义一致：开启工作区隔离时相对路径相对当前
frontend/user 工作区；否则相对 file_tools.base_dir（通常为项目根）。
供 AgentKernel / call_tool 在调用工具前注入，避免 LLM 只传工作区相对路径时
memory_ingest、媒体解析等未走 file_tools 的路径失败。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from agent_core.config import Config, FileToolsConfig, get_config
from agent_core.agent.session_paths import expand_user_path_str_for_session


def resolve_workspace_root_for_exec_ctx(exec_ctx: dict, config: Config) -> Path:
    """当前 frontend/user 的工作区根目录（与 file_tools / bash 笼一致）。"""
    from agent_core.agent.memory_paths import (
        effective_memory_namespace_from_execution_context,
    )
    from agent_core.agent.workspace_paths import resolve_workspace_owner_dir

    src, uid = effective_memory_namespace_from_execution_context(exec_ctx)
    return Path(
        resolve_workspace_owner_dir(config.command_tools, uid, source=src)
    ).expanduser().resolve()


def resolve_path_string_for_tool(
    path_str: str,
    config: Config,
    exec_ctx: Optional[dict],
    *,
    file_tools_config: Optional[FileToolsConfig] = None,
) -> Tuple[Optional[Path], Optional[str]]:
    """
    解析工具使用的本地文件路径（读语义）。

    绝对路径：直接 resolve；相对路径：按工作区隔离策略拼到工作区或 base_dir。
    """
    ft_cfg = file_tools_config or getattr(config, "file_tools", None)
    if ft_cfg is None:
        ft_cfg = FileToolsConfig()

    ctx = exec_ctx or {}
    expanded = expand_user_path_str_for_session(path_str, config, exec_ctx=ctx)
    if getattr(config.command_tools, "workspace_isolation_enabled", False):
        workspace_root = resolve_workspace_root_for_exec_ctx(ctx, config)
        try:
            raw = Path(expanded).expanduser()
            candidate = (
                raw.resolve() if raw.is_absolute() else (workspace_root / raw).resolve()
            )
            return candidate, None
        except (OSError, ValueError) as e:
            return None, f"无效路径: {e}"
    base = Path(ft_cfg.base_dir).resolve()
    try:
        raw = Path(expanded).expanduser()
        resolved = raw.resolve() if raw.is_absolute() else (base / raw).resolve()
        return resolved, None
    except (OSError, ValueError) as e:
        return None, f"无效路径: {e}"


def _should_skip_path_string(s: str) -> bool:
    t = s.strip()
    if not t:
        return True
    if t.startswith(("http://", "https://", "data:")):
        return True
    return False


def apply_workspace_path_resolution_to_tool_args(
    tool_name: str,
    arguments: Dict[str, Any],
    config: Optional[Config] = None,
) -> Dict[str, Any]:
    """
    在注入 __execution_context__ 之后调用，将已知文件类参数转为绝对路径字符串。

    解析失败时保留原值，由具体工具报错。
    """
    if not isinstance(arguments, dict):
        return arguments
    cfg = config if config is not None else get_config()

    exec_ctx = arguments.get("__execution_context__")
    if not isinstance(exec_ctx, dict):
        exec_ctx = {}

    out = dict(arguments)

    if tool_name == "call_tool":
        inner_name = out.get("name")
        inner_args = out.get("arguments")
        if (
            isinstance(inner_name, str)
            and inner_name.strip()
            and isinstance(inner_args, dict)
        ):
            inner_merged = dict(inner_args)
            if "__execution_context__" not in inner_merged:
                inner_merged["__execution_context__"] = dict(exec_ctx)
            out["arguments"] = apply_workspace_path_resolution_to_tool_args(
                inner_name.strip(), inner_merged, cfg
            )
        return out

    def _resolve_value(val: Any) -> Any:
        if not isinstance(val, str) or _should_skip_path_string(val):
            return val
        resolved, err = resolve_path_string_for_tool(val.strip(), cfg, exec_ctx)
        if err or resolved is None:
            return val
        return str(resolved)

    if tool_name == "attach_media":
        if "path" in out and out["path"] is not None:
            out["path"] = _resolve_value(out["path"])
        paths = out.get("paths")
        if isinstance(paths, list):
            out["paths"] = [
                _resolve_value(p) if isinstance(p, str) else p for p in paths
            ]
        return out

    if tool_name == "attach_image_to_reply" and out.get("image_path"):
        out["image_path"] = _resolve_value(out["image_path"])
        return out

    if tool_name in ("read_file", "write_file", "modify_file") and "path" in out:
        out["path"] = _resolve_value(out["path"])
        return out

    if tool_name == "memory_ingest" and out.get("file_path"):
        out["file_path"] = _resolve_value(out["file_path"])
        return out

    return out
