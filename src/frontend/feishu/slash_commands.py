"""飞书斜杠指令处理。

与 CLI interactive.py 的 /clear、/usage、/session、/help 等指令保持一致，
在发送给 Agent 前拦截并执行 IPC 方法，将结果以文本形式返回。
"""

from __future__ import annotations

import inspect
import shlex
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from agent_core.config import get_config

if TYPE_CHECKING:
    from system.automation.ipc import AutomationIPCClient

logger = __import__("logging").getLogger(__name__)

_DEFAULT_SESSION_LIST_LIMIT = 30


def _string_attr(obj: Any, name: str, default: str = "") -> str:
    value = getattr(obj, name, default)
    return value.strip() if isinstance(value, str) else default


def _int_attr(obj: Any, name: str, default: int) -> int:
    value = getattr(obj, name, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _infer_feishu_base_session_id(session_id: str) -> str:
    sid = (session_id or "").strip()
    if sid.startswith(("feishu:user:", "feishu:chat:")):
        parts = sid.split(":")
        if len(parts) >= 3:
            return ":".join(parts[:3])
    return ""


def _feishu_base_session_id(client: Any) -> str:
    explicit = _string_attr(client, "feishu_base_session_id")
    if explicit:
        return explicit
    return _infer_feishu_base_session_id(_string_attr(client, "active_session_id"))


def _new_session_id(client: Any) -> str:
    base = _feishu_base_session_id(client)
    if base:
        return f"{base}:{int(time.time())}"
    return f"feishu:{int(time.time())}"


def _session_in_scope(session_id: str, *, base_session_id: str, active: str) -> bool:
    sid = (session_id or "").strip()
    if not sid:
        return False
    if active and sid == active:
        return True
    if base_session_id:
        return sid == base_session_id or sid.startswith(f"{base_session_id}:")
    return sid.startswith("feishu:")


def _scoped_sessions_for_display(
    client: Any, sessions: List[str]
) -> tuple[List[str], int, bool]:
    active = _string_attr(client, "active_session_id")
    base_session_id = _feishu_base_session_id(client)
    source = _string_attr(client, "source")
    should_scope = bool(base_session_id) or source == "feishu"

    if should_scope:
        scoped = [
            sid
            for sid in sessions
            if _session_in_scope(sid, base_session_id=base_session_id, active=active)
        ]
        if active and active not in scoped:
            scoped.insert(0, active)
    else:
        scoped = list(sessions)

    deduped: List[str] = []
    seen = set()
    for sid in scoped:
        if sid in seen:
            continue
        seen.add(sid)
        deduped.append(sid)

    limit = _int_attr(client, "session_list_limit", _DEFAULT_SESSION_LIST_LIMIT)
    if len(deduped) <= limit:
        return deduped, 0, should_scope

    shown = deduped[:limit]
    if active and active in deduped and active not in shown and shown:
        shown[-1] = active
    return shown, len(deduped) - len(shown), should_scope


async def _expire_active_session_before_new(
    client: "AutomationIPCClient", target_session_id: str
) -> Optional[str]:
    active = _string_attr(client, "active_session_id")
    target = (target_session_id or "").strip()
    if not active or active == target:
        return None
    expire = getattr(client, "expire_session", None)
    if not callable(expire) or not inspect.iscoroutinefunction(expire):
        return None
    try:
        await expire(active, reason="manual_new")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "failed to expire active session before feishu /new (session_id=%s): %s",
            active,
            exc,
        )
        return str(exc) or type(exc).__name__
    return None


def _format_token_usage(u: Dict[str, Any]) -> str:
    """将 token 用量格式化为简短文本（适合飞书消息）。"""
    cost_str = ""
    if u.get("cost_yuan") is not None:
        try:
            cost_str = f"，约 ¥{float(u['cost_yuan']):.4f}"
        except (TypeError, ValueError):
            pass
    lines = [
        f"调用次数: {u.get('call_count', 0)}",
        f"输入 token: {u.get('prompt_tokens', 0):,}",
        f"输出 token: {u.get('completion_tokens', 0):,}",
        f"合计 token: {u.get('total_tokens', 0):,}{cost_str}",
    ]
    hit = int(u.get("prompt_cache_hit_tokens") or 0)
    miss = int(u.get("prompt_cache_miss_tokens") or 0)
    if hit > 0 or miss > 0:
        lines.append(f"输入缓存命中 token: {hit:,}")
        lines.append(f"输入缓存未命中 token: {miss:,}")
    ctx_max = u.get("context_window_max_tokens")
    ctx_cur = u.get("context_window_current_tokens")
    ctx_rem = u.get("context_window_remaining_tokens")
    if (
        isinstance(ctx_max, int)
        and ctx_max > 0
        and isinstance(ctx_cur, int)
        and isinstance(ctx_rem, int)
    ):
        lines.append(
            f"上下文窗口: 当前 {ctx_cur:,} / 最大 {ctx_max:,}，剩余 {ctx_rem:,} token"
        )
    return "\n".join(lines)


def _goal_help_text() -> str:
    return """Goal 指令（Agent 会话内工作目标）：
/goal <instruction>  创建目标并开始执行（例：/goal 重构 auth 模块并补测试）
/goal list           列出当前活跃目标
/goal help           显示此帮助

与用户待办 add_task 不同：goal 是 Agent 当前会话的工作计划。"""


def _format_goal_create_result(res: Dict[str, Any]) -> str:
    goal = res.get("goal") if isinstance(res.get("goal"), dict) else {}
    gid = str(goal.get("id") or "—")
    title = str(goal.get("title") or "—")
    lines = [f"已创建目标 {gid}", f"标题: {title}"]
    if res.get("autostart_queued"):
        lines.append("Agent 已开始执行此目标。")
    return "\n".join(lines)


def _format_goal_list_result(res: Dict[str, Any]) -> str:
    if not res.get("session_loaded", True):
        return "当前会话尚未加载，请先发送任意消息或执行 /goal <instruction>。"
    goals_raw = res.get("goals")
    goals = list(goals_raw) if isinstance(goals_raw, list) else []
    if not goals:
        return "当前没有活跃的 Agent 目标。"
    lines = ["当前 Agent 目标："]
    for g in goals:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or "—")
        title = str(g.get("title") or "—")
        status = str(g.get("status") or "active")
        lines.append(f"- {gid} [{status}] {title}")
        steps = g.get("steps") or []
        if isinstance(steps, list):
            for step in steps[:5]:
                if isinstance(step, dict):
                    lines.append(
                        f"    · [{step.get('status', '?')}] {step.get('description', '')}"
                    )
    return "\n".join(lines)


def _help_text() -> str:
    return """可用指令：
/clear - 清空对话历史
/goal <instruction> - 创建 Agent 工作目标并开始执行
/goal list - 列出当前活跃目标
/interrupt 或 /cancel 或 /stop - 中断当前正在执行的内核任务（等同 CLI 中 Ctrl+C 正在处理时）
/compress [N] - 主动折叠上下文为摘要（可选 N 指定保留最近几轮）
/usage 或 /stats - 本会话 token 用量
/model 或 /model list - 列出可用 LLM（★ 为当前主对话）
/model <备注名或配置名> - 切换主对话模型（与配置里 label 一致即可，可含空格）
/session - 显示当前会话
/session list - 列出已加载会话
/session switch <id> - 切换到指定会话
/session new [id] - 创建并切换到新会话
/session delete <id> - 删除会话记录
/new [id] - 快速新开 core 对话（等价 /session new [id]）
/remote-use <login> [path] [--profile dev] - 将当前会话切换到远程工作区模式
/remote-status - 查看当前会话远程工作区状态
/remote-release 或 /cloud-use - 释放远程工作区，恢复云端工作区
/mcp list - 列出已声明的 MCP（location / attach_on / 是否已挂）
/mcp attach <name> - 将配置中的 MCP 挂到当前会话
/mcp detach <name> - 从当前会话卸下 MCP
/mcp reload <name> - 重新加载 MCP
/dangerously on|off|status - 切换危险放行模式（授权用户可跳过人工审批）
/help - 显示此帮助"""


def _can_toggle_dangerous_mode(client: Any) -> bool:
    cfg = get_config().feishu

    # 统一用户名检查（dashboard / 未来多端共用）
    username = _string_attr(client, "username")
    if username:
        allow_usernames = {
            str(v).strip()
            for v in getattr(cfg, "dangerous_mode_allowed_usernames", []) or []
            if str(v).strip()
        }
        if username in allow_usernames:
            return True

    # 飞书特有 ID 检查（向后兼容，未来统一用户名后废弃）
    allow_open_ids = {
        str(v).strip()
        for v in getattr(cfg, "dangerous_mode_allowed_open_ids", []) or []
        if str(v).strip()
    }
    allow_user_ids = {
        str(v).strip()
        for v in getattr(cfg, "dangerous_mode_allowed_user_ids", []) or []
        if str(v).strip()
    }
    open_id = _string_attr(client, "feishu_open_id")
    user_id = _string_attr(client, "feishu_user_id")
    if open_id and open_id in allow_open_ids:
        return True
    if user_id and user_id in allow_user_ids:
        return True
    return False


def _parse_ttl_seconds(value: str) -> Optional[int]:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    unit = raw[-1]
    number = raw[:-1] if unit in {"s", "m", "h"} else raw
    try:
        n = int(number)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if unit == "h":
        return n * 3600
    if unit == "m":
        return n * 60
    return n


def _parse_remote_use_args(
    cmd_text: str,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        tokens = shlex.split(cmd_text)
    except ValueError as exc:
        return None, f"解析失败: {exc}"
    if len(tokens) < 2:
        return (
            None,
            "用法: /remote-use <login> [path] [--profile strict|dev|host-user|host-admin] [--ttl 30m]",
        )
    login = tokens[1].strip()
    path = "~"
    profile = "dev"
    ttl_seconds: Optional[int] = None
    i = 2
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--profile":
            if i + 1 >= len(tokens):
                return None, "缺少 --profile 的值"
            profile = tokens[i + 1].strip()
            i += 2
            continue
        if tok.startswith("--profile="):
            profile = tok.split("=", 1)[1].strip()
            i += 1
            continue
        if tok == "--ttl":
            if i + 1 >= len(tokens):
                return None, "缺少 --ttl 的值"
            ttl_seconds = _parse_ttl_seconds(tokens[i + 1])
            if ttl_seconds is None:
                return None, "ttl 格式应为正整数秒，或 30m / 2h"
            i += 2
            continue
        if tok.startswith("--ttl="):
            ttl_seconds = _parse_ttl_seconds(tok.split("=", 1)[1])
            if ttl_seconds is None:
                return None, "ttl 格式应为正整数秒，或 30m / 2h"
            i += 1
            continue
        if tok.startswith("--"):
            return None, f"未知参数: {tok}"
        path = tok
        i += 1
    if profile not in {"strict", "dev", "host-user", "host-admin"}:
        return None, "profile 必须是 strict、dev、host-user 或 host-admin"
    return {
        "login": login,
        "path": path,
        "profile": profile,
        "ttl_seconds": ttl_seconds,
    }, None


def _format_remote_state(state: Dict[str, Any]) -> str:
    if not state:
        return "远程工作区未启用。"
    path = state.get("resolved_path") or state.get("requested_path") or "—"
    mount = state.get("workspace_mount") or "/workspace"
    login = state.get("login") or "—"
    profile = state.get("profile") or "—"
    return (
        "远程工作区已启用\n"
        f"登录: {login}\n"
        f"权限档位: {profile}\n"
        f"工作区: {mount} -> {path}\n"
        "提示: 已写入对话历史的 [工作区切换] 通知；Available Skills 索引来自远程工作区。"
    )


def _format_compress_result(res: Dict[str, Any]) -> str:
    """把 ``/compress`` 返回的结构化结果格式化为飞书可读纯文本。"""
    if not res.get("session_loaded", True):
        return (
            "上下文压缩：当前会话尚未在 daemon 内驻留，无法压缩。\n"
            "请先发送任意消息触发加载，再执行 /compress。"
        )

    before = int(res.get("messages_before", 0) or 0)
    after = int(res.get("messages_after", 0) or 0)
    kept = int(res.get("kept", after) or after)
    cur = int(res.get("current_tokens", 0) or 0)
    th = int(res.get("threshold_tokens", 0) or 0)
    chars = int(res.get("summary_chars", 0) or 0)
    rounds = int(res.get("compression_round", 0) or 0)
    model = str(res.get("model") or "—")
    status = "已压缩" if res.get("compressed") else "未触发压缩（消息数不足以折叠）"

    return (
        "上下文压缩\n"
        f"状态: {status}\n"
        f"消息数: {before} → {after}（保留 {kept} 条）\n"
        f"摘要长度: {chars} 字符\n"
        f"触发时上下文: {cur:,} token  /  阈值 {th:,} token\n"
        f"当前模型: {model}\n"
        f"累计压缩轮次: {rounds}"
    )


async def try_handle_slash_command(
    client: "AutomationIPCClient",
    text: str,
) -> Tuple[bool, Optional[str]]:
    """
    尝试处理斜杠指令。

    Args:
        client: 已 switch_session 到目标会话的 IPC 客户端
        text: 用户输入文本

    Returns:
        (handled, reply_text)
        - handled=True 时 reply_text 为返回给飞书的消息
        - handled=False 时 reply_text 为 None，调用方应继续走 send_message
    """
    raw = (text or "").strip()
    if not raw:
        return False, None

    # 支持 / 前缀或直接命令（与 CLI 对齐）
    cmd_text = raw[1:].strip() if raw.startswith("/") else raw
    if not cmd_text:
        return False, None

    parts = cmd_text.split()
    cmd_lower = parts[0].lower()

    # /clear
    if cmd_lower == "clear":
        await client.clear_context()
        return True, "对话历史已清空。"

    # /interrupt | /cancel | /stop — 对齐 CLI 在处理中按 Ctrl+C：取消内核 inflight，不销毁会话
    if cmd_lower in ("interrupt", "cancel", "stop"):
        sid = str(getattr(client, "active_session_id", "") or "").strip()
        if not sid:
            return True, "无法解析当前会话 ID，请先发一条普通消息再试。"
        try:
            cancelled = await client.terminal_cancel(sid)
        except Exception as exc:
            return True, f"中断失败: {exc}"
        if cancelled:
            return (
                True,
                "已中断当前会话。",
            )
        return (
            True,
            "No active chat session.",
        )

    # /compress [N]
    if cmd_lower == "compress":
        keep_arg: Optional[int] = None
        if len(parts) >= 2:
            try:
                keep_arg = max(1, int(parts[1]))
            except (TypeError, ValueError):
                return True, "用法: /compress [N]   N 为保留最近几轮（正整数）"
        try:
            res = await client.compress_context(keep_arg)
        except Exception as exc:
            return True, f"压缩失败: {exc}"
        return True, _format_compress_result(res or {})

    if cmd_lower in ("dangerously", "danger", "dangerous"):
        action = parts[1].strip().lower() if len(parts) > 1 else "status"
        if action not in {"on", "off", "status"}:
            return True, "Usage: /dangerously on|off|status"
        if action in {"on", "off"} and not _can_toggle_dangerous_mode(client):
            return True, (
                "Permission denied: your account is not allowed to toggle dangerous mode."
            )
        try:
            if action == "status":
                status = await client.get_dangerous_mode()
            else:
                status = await client.set_dangerous_mode(enabled=(action == "on"))
        except Exception as exc:
            return True, f"Failed to update dangerous mode: {exc}"
        enabled_now = bool(status.get("dangerous_mode_enabled"))
        sid = str(status.get("session_id") or getattr(client, "active_session_id", ""))
        return True, (
            "Dangerous mode is ENABLED.\n"
            "Human approval is bypassed for permission checks in this session.\n"
            f"Session: {sid}"
            if enabled_now
            else "Dangerous mode is DISABLED.\n"
            "Human approval is required again.\n"
            f"Session: {sid}"
        )

    # /goal — 直接创建 Agent 工作目标
    if cmd_lower == "goal" or (parts and parts[0].lower() == "goal"):
        sub = cmd_text.split(maxsplit=1)[1].strip() if len(parts) > 1 else ""
        sub_l = sub.lower()
        if not sub or sub_l in ("help", "-h", "?"):
            return True, _goal_help_text()
        if sub_l in ("list", "ls"):
            try:
                res = await client.list_goals()
            except Exception as exc:
                return True, f"列出目标失败: {exc}"
            return True, _format_goal_list_result(res or {})
        raw_chat = getattr(client, "feishu_chat_id", "")
        chat_id = raw_chat.strip() if isinstance(raw_chat, str) else ""
        try:
            res = await client.create_goal(
                sub,
                autostart=True,
                feishu_chat_id=chat_id or None,
            )
        except Exception as exc:
            return True, f"创建目标失败: {exc}"
        return True, _format_goal_create_result(res or {})

    # /help
    if cmd_lower == "help":
        return True, _help_text()

    # /usage, /stats, /tokens
    if cmd_lower in ("usage", "stats", "tokens"):
        u = await client.get_token_usage()
        if not isinstance(u, dict):
            u = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "call_count": 0,
            }
        return True, "本会话 Token 用量：\n" + _format_token_usage(u)

    if cmd_lower in ("remote-status", "remote status"):
        try:
            res = await client.remote_workspace_status()
        except Exception as exc:
            return True, f"远程工作区状态读取失败: {exc}"
        state = res.get("state") if isinstance(res, dict) else None
        return True, _format_remote_state(state if isinstance(state, dict) else {})

    if cmd_lower in ("remote-release", "cloud-use", "remote release"):
        try:
            res = await client.remote_workspace_release()
        except Exception as exc:
            return True, f"释放远程工作区失败: {exc}"
        released = bool(res.get("released")) if isinstance(res, dict) else False
        if released:
            return True, "已释放远程工作区，当前会话将恢复云端工作区模式。"
        return True, "当前会话未启用远程工作区。"

    if cmd_lower == "mcp" or cmd_lower.startswith("mcp "):
        parts = cmd_text.split()
        # cmd_text is without leading slash; first token may be "mcp"
        tokens = parts[1:] if parts and parts[0].lower() == "mcp" else parts
        sub = (tokens[0].lower() if tokens else "list").strip()
        name = " ".join(tokens[1:]).strip() if len(tokens) > 1 else ""
        try:
            if sub in ("list", "ls", ""):
                res = await client.mcp_list()
                servers = res.get("servers") if isinstance(res, dict) else None
                if not servers:
                    return True, "未配置任何 MCP server（或 mcp.enabled=false）。"
                lines = ["MCP servers:"]
                for s in servers:
                    if not isinstance(s, dict):
                        continue
                    mark = "ON" if s.get("attached") else "off"
                    lines.append(
                        f"- {s.get('name')} [{mark}] location={s.get('location')} "
                        f"attach_on={s.get('attach_on')} tools={s.get('tool_count', 0)}"
                    )
                    if s.get("error"):
                        lines.append(f"    error: {s.get('error')}")
                return True, "\n".join(lines)
            if sub == "attach":
                if not name:
                    return True, "用法: /mcp attach <server_name>"
                res = await client.mcp_attach(server_name=name)
                if res.get("ok"):
                    tools = res.get("attached_tools") or []
                    return True, f"已挂载 {name}，工具数 {len(tools)}。"
                return True, f"挂载失败: {res.get('error') or 'unknown'}"
            if sub == "detach":
                if not name:
                    return True, "用法: /mcp detach <server_name>"
                res = await client.mcp_detach(server_name=name)
                if res.get("ok"):
                    return True, f"已卸载 {name}。"
                return True, f"卸载失败: {res.get('error') or 'unknown'}"
            if sub == "reload":
                if not name:
                    return True, "用法: /mcp reload <server_name>"
                res = await client.mcp_reload(server_name=name)
                if res.get("ok"):
                    tools = res.get("attached_tools") or []
                    return True, f"已重载 {name}，工具数 {len(tools)}。"
                return True, f"重载失败: {res.get('error') or 'unknown'}"
            return True, "用法: /mcp list|attach|detach|reload"
        except Exception as exc:
            return True, f"MCP 命令失败: {exc}"

    if cmd_lower.startswith("remote-use"):
        parsed, err = _parse_remote_use_args(cmd_text)
        if err:
            return True, err
        assert parsed is not None
        try:
            state = await client.remote_workspace_use(**parsed)
        except Exception as exc:
            return True, f"切换远程工作区失败: {exc}"
        return True, _format_remote_state(state)

    # /model — 与 CLI 一致：list 或按 label/配置名切换（走 IPC model_list / model_switch）
    if cmd_lower == "model":
        rest = cmd_text.split(maxsplit=1)
        sub = rest[1].strip() if len(rest) > 1 else ""
        sub_tokens = sub.split()
        if sub_tokens and sub_tokens[0].lower() == "switch":
            sub = " ".join(sub_tokens[1:]).strip()
        sub_l = sub.lower()
        if not sub or sub_l in ("list", "ls"):
            try:
                models = await client.list_models()
            except Exception as exc:
                return True, f"列出模型失败: {exc}"
            if not models:
                return True, "当前没有可用的 LLM provider 配置。"
            # 勿用 ASCII * 作行首标记：飞书会把 * 解析成 Markdown 列表，当前行前会多出空行。
            lines = [
                "Available models ( ★ = current active model, V = vision provider ); switch: /model <model name>",
                "Example: /model Qwen3.5 Plus",
            ]
            for m in models:
                mark = "★" if m.get("is_active") else " "
                vp = "V" if m.get("is_vision_provider") else " "
                raw_label = m.get("label")
                display = (
                    str(raw_label).strip()
                    if raw_label not in (None, "")
                    else str(m.get("name") or "—")
                )
                display = " ".join(display.split())
                caps_bits = []
                if m.get("vision"):
                    caps_bits.append("vision")
                if m.get("function_calling"):
                    caps_bits.append("tools")
                cap_s = ",".join(caps_bits) if caps_bits else "—"
                lines.append(f"  {mark}{vp}  {display:<40}  {cap_s}")
            lines.append("示例: /model Kimi K2.5")
            return True, "\n".join(lines)
        try:
            info = await client.switch_model(sub)
        except Exception as exc:
            return True, f"切换模型失败: {exc}"
        if isinstance(info, dict) and info.get("name"):
            api_id = info.get("api_model") or info.get("model")
            vf = "支持视觉" if info.get("vision") else "无视觉"
            vp = info.get("vision_provider") or "—"
            return True, (
                f"已切换主对话 provider: {info.get('name')}\n"
                f"API 模型 ID: {api_id}\n{vf}  |  vision_provider={vp}"
            )
        return True, f"已请求切换: {sub}"

    # /new [id] -> /session new [id]
    if cmd_lower == "new":
        session_id = (
            parts[1].strip()
            if len(parts) > 1 and parts[1].strip()
            else _new_session_id(client)
        )
        cut_error = await _expire_active_session_before_new(client, session_id)
        if cut_error:
            return True, f"新建会话前保存当前会话记忆失败: {cut_error}"
        created = await client.switch_session(session_id, create_if_missing=True)
        if created:
            return True, f"已创建并切换到新会话: {session_id}"
        return True, f"会话已存在，已切换: {session_id}"

    # /session 系列
    if cmd_lower != "session":
        return False, None

    sub = parts[1].lower() if len(parts) > 1 else "show"

    if sub in ("show", "current"):
        active = getattr(client, "active_session_id", "unknown")
        return True, f"当前会话: {active}"

    if sub == "whoami":
        owner = getattr(client, "owner_id", "root")
        source = getattr(client, "source", "feishu")
        active = getattr(client, "active_session_id", "unknown")
        return True, f"user={owner} source={source} session={active}"

    if sub in ("list", "ls"):
        sessions: List[str] = await client.list_sessions()
        active = getattr(client, "active_session_id", "")
        sessions, omitted, scoped = _scoped_sessions_for_display(client, sessions)
        if not sessions:
            return True, "当前没有会话。"
        lines = ["已加载会话（当前飞书窗口）:" if scoped else "已加载会话:"]
        for sid in sessions:
            marker = " *" if sid == active else ""
            lines.append(f"  - {sid}{marker}")
        if omitted > 0:
            lines.append(f"  ... 还有 {omitted} 个会话未显示")
        return True, "\n".join(lines)

    if sub == "switch":
        if len(parts) < 3 or not parts[2].strip():
            return True, "用法: /session switch <id>"
        target = parts[2].strip()
        sessions = await client.list_sessions()
        if target not in sessions:
            return (
                True,
                f"会话不存在: {target}\n可用 /session list 查看，或 /session new <id> 创建。",
            )
        await client.switch_session(target, create_if_missing=False)
        return True, f"已切换到会话: {target}"

    if sub == "new":
        session_id = (
            parts[2].strip()
            if len(parts) > 2 and parts[2].strip()
            else _new_session_id(client)
        )
        cut_error = await _expire_active_session_before_new(client, session_id)
        if cut_error:
            return True, f"新建会话前保存当前会话记忆失败: {cut_error}"
        created = await client.switch_session(session_id, create_if_missing=True)
        if created:
            return True, f"已创建并切换到新会话: {session_id}"
        return True, f"会话已存在，已切换: {session_id}"

    if sub == "delete":
        if len(parts) < 3 or not parts[2].strip():
            return True, "用法: /session delete <id>"
        target = parts[2].strip()
        ok = await client.delete_session(target)
        if ok:
            return True, f"已删除会话记录: {target}"
        return True, f"无法删除会话: {target}（可能是当前活跃会话或不存在）"

    return (
        True,
        "用法: /session | /session list | /session switch <id> | /session new [id] | /session delete <id> | /new [id]",
    )
