"""飞书斜杠指令处理。

与 CLI interactive.py 的 /clear、/usage、/session、/help 等指令保持一致，
在发送给 Agent 前拦截并执行 IPC 方法，将结果以文本形式返回。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from system.automation.ipc import AutomationIPCClient

logger = __import__("logging").getLogger(__name__)


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


def _help_text() -> str:
    return """可用指令：
/clear - 清空对话历史
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
/help - 显示此帮助"""


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
            else f"feishu:{int(time.time())}"
        )
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
        if not sessions:
            return True, "当前没有会话。"
        lines = ["已加载会话:"]
        for sid in sessions:
            marker = " *" if sid == active else ""
            lines.append(f"  - {sid}{marker}")
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
            else f"feishu:{int(time.time())}"
        )
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
