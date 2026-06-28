"""
CLI 交互式界面

包含欢迎信息、帮助、token 用量展示以及主交互循环。
"""

import asyncio
import inspect
import json
import os
import shlex
import signal
import sys
import shutil
import threading
import time
from typing import Any, List, Optional

from agent_core.interfaces import AgentHooks, AgentRunInput
from system.automation.repositories import _automation_base_dir
from agent_core.utils.cli_style import (
    hint,
    label,
    accent,
    t,
    prompt_prefix,
    thin_separator,
    status_bar,
)

_PromptSession: Any = None
HTML: Any = None
_patch_stdout: Any = None
try:
    from prompt_toolkit import PromptSession as _PromptSession
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.patch_stdout import patch_stdout as _patch_stdout

    _HAS_PROMPT_TOOLKIT = True
except ImportError:
    _HAS_PROMPT_TOOLKIT = False

Console: Any = None
Live: Any = None
Markdown: Any = None
try:
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown

    _HAS_RICH = True
    _RICH_CONSOLE: Any = Console()
except Exception:  # pragma: no cover
    _HAS_RICH = False
    _RICH_CONSOLE = None


def print_welcome():
    """打印欢迎信息"""
    md = """
    ╔══════════════════════════════════════════════════════╗
    ║ Greetings!                                           ║
    ╟──────────────────────────────────────────────────────╢
    ║ ░█▀▄░█──░█░█─▄─▄─░█▀▄─▄▀▀                            ║
    ║ ░█─█░█──░█░█─█─█─░█─█─▀▀▄                            ║
    ║ ░█▄▀─▀▀──▀─▀─▀─▀─░█▀─░▀▀▀                            ║
    ║                                 MACCHIATO            ║
    ╚══════════════════════════════════════════════════════╝"""
    if _HAS_RICH and _RICH_CONSOLE is not None:
        _RICH_CONSOLE.print(Markdown(md))
    else:
        print()
        print("=" * 50)
        print("  macchiatoBot - tool-driven LLM assistant")
        print("=" * 50)
        print()
        print("你好！我是你的日程管理助手，可以帮助你：")
        print("  • 添加日程事件（会议、约会等）")
        print("  • 创建待办任务")
        print("  • 查询日程和任务")
        print("  • 智能规划时间")
        print()
        print(
            "命令： quit/exit 退出  |  clear 清空对话  |  help 帮助  |  usage/stats 用量"
        )
        print("-" * 50)
        print()


def print_help():
    """打印帮助信息"""
    md = """
# 帮助信息

## 可用命令

- `/quit` / `exit` &nbsp;&nbsp;退出程序
- `/clear` &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;清空对话历史
- `/goal <instruction>` &nbsp;创建 Agent 工作目标并开始执行
- `/goal list` &nbsp;列出当前活跃目标
- `/compress [N]` &nbsp;&nbsp;主动折叠上下文为摘要；可选 `N` 指定保留最近几轮
- `/help` &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;显示此帮助
- `/usage` / `/stats` &nbsp;&nbsp;本会话 token 用量
- `/interrupt` / `/cancel` / `/stop` &nbsp;&nbsp;中断当前正在执行的内核任务
- `/session` &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;显示当前会话
- `/session whoami` &nbsp;显示当前 user/source/session
- `/session list` &nbsp;列出当前已加载会话
- `/session switch <id>` &nbsp;切换到指定会话（已存在）
- `/session new [id]` &nbsp;创建并切换到新会话
- `/session delete <id>` &nbsp;删除会话记录（仅从会话列表移除，不删历史）
- `/new [id]` &nbsp;快速新开 core 对话（先保存当前会话记忆）
- `/model` / `/model list` &nbsp;列出各模型的 **备注 label（切换用）** 与能力；当前主对话标 `*`，vision 回落标 `V`
- `/model <model name>` &nbsp;切换：与列表中的展示名一致（一般为 label；无 label 时为配置名）
- `/model switch <model name>` &nbsp;与上一行相同（多写 `switch` 亦可）
- `/remote-use <login> [path] [--profile dev]` &nbsp;&nbsp;切换到远程工作区模式
- `/remote-status` &nbsp;&nbsp;查看当前远程工作区状态
- `/remote-release` &nbsp;&nbsp;释放远程工作区，恢复云端工作区
- `/dangerously on|off|status` &nbsp;&nbsp;切换危险放行模式（跳过人工审批）

## 示例对话

- 明天下午3点有个团队会议
- 添加一个任务：完成项目报告，预计2小时，周五前完成
- 查看今天的日程
- 查看我的待办任务
- 帮我规划一下明天的任务
"""
    if _HAS_RICH and _RICH_CONSOLE is not None:
        _RICH_CONSOLE.print(Markdown(md))
    else:
        print()
        print("=" * 50)
        print("  帮助信息")
        print("=" * 50)
        print()
        print("可用命令:")
        print("  /quit 或 exit        退出程序")
        print("  /clear               清空对话历史")
        print("  /goal <instruction>  创建 Agent 工作目标并开始执行")
        print("  /goal list           列出当前活跃目标")
        print("  /compress [N]        主动折叠上下文为摘要（可选 N 指定保留最近几轮）")
        print("  /help                显示此帮助")
        print("  /usage 或 /stats     本会话 token 用量")
        print("  /interrupt           中断当前正在执行的内核任务")
        print("  /session             查看当前会话")
        print("  /session whoami")
        print("  /session list")
        print("  /session switch <id>")
        print("  /session new [id]")
        print("  /session delete <id> 删除会话记录")
        print("  /new [id]            快速新开 core 对话")
        print("  /model               列出或切换模型")
        print("  /remote-use          切换到远程工作区")
        print("  /remote-status       查看远程工作区状态")
        print("  /remote-release      释放远程工作区")
        print("  /dangerously         切换危险放行模式")
        print()
        print("示例对话:")
        print("  • 明天下午3点有个团队会议")
        print("  • 添加一个任务：完成项目报告，预计2小时，周五前完成")
        print("  • 查看今天的日程")
        print("  • 查看我的待办任务")
        print("  • 帮我规划一下明天的任务")
        print("-" * 50)
        print()


def print_token_usage_data(u: dict):
    """打印本会话 token 用量统计。u 应包含 call_count, prompt_tokens, completion_tokens, total_tokens 等。"""
    call_count = u.get("call_count", 0)
    prompt_tokens = u.get("prompt_tokens", 0)
    completion_tokens = u.get("completion_tokens", 0)
    total_tokens = u.get("total_tokens", 0)
    cost_line = (
        f"\n- **预估费用**: `¥{u['cost_yuan']:.4f}`"
        if u.get("cost_yuan") is not None
        else ""
    )

    # 上下文窗口使用情况（如果后端提供）
    ctx_max = u.get("context_window_max_tokens")
    ctx_cur = u.get("context_window_current_tokens")
    ctx_rem = u.get("context_window_remaining_tokens")
    if (
        isinstance(ctx_max, int)
        and ctx_max > 0
        and isinstance(ctx_cur, int)
        and isinstance(ctx_rem, int)
    ):
        ctx_line = f"\n- **上下文窗口**: `当前 {ctx_cur:,} / 最大 {ctx_max:,}，剩余 {ctx_rem:,} token`"
    else:
        ctx_line = ""
    hit = int(u.get("prompt_cache_hit_tokens") or 0)
    miss = int(u.get("prompt_cache_miss_tokens") or 0)
    cache_md = ""
    if hit > 0 or miss > 0:
        cache_md = f"\n- **输入缓存命中 token**: `{hit}`\n- **输入缓存未命中 token**: `{miss}`"

    md = f"""
# Token 用量统计

- **调用次数**: `{call_count}`
- **输入 token**: `{prompt_tokens}`
- **输出 token**: `{completion_tokens}`
- **合计 token**: `{total_tokens}`{cost_line}{cache_md}{ctx_line}
"""
    if _HAS_RICH and _RICH_CONSOLE is not None:
        _RICH_CONSOLE.print(Markdown(md))
    else:
        print()
        print("=" * 50)
        print("  本会话 Token 用量统计")
        print("=" * 50)
        print(f"  调用次数:     {u.get('call_count', 0)}")
        print(f"  输入 token:   {u.get('prompt_tokens', 0)}")
        print(f"  输出 token:   {u.get('completion_tokens', 0)}")
        print(f"  合计 token:   {u.get('total_tokens', 0)}")
        if hit > 0 or miss > 0:
            print(f"  输入缓存命中 token:   {hit}")
            print(f"  输入缓存未命中 token: {miss}")
        if u.get("cost_yuan") is not None:
            print(f"  预估费用:     ¥{u['cost_yuan']:.4f}")
        if (
            isinstance(ctx_max, int)
            and ctx_max > 0
            and isinstance(ctx_cur, int)
            and isinstance(ctx_rem, int)
        ):
            print(
                f"  上下文窗口:   当前 {ctx_cur:,} / 最大 {ctx_max:,}，剩余 {ctx_rem:,} token"
            )
        print("=" * 50)
        print()


def _print_compress_result(res: dict) -> None:
    """渲染 ``/compress`` 命令返回的结构化结果。

    与 ``print_token_usage_data`` 风格一致：rich 渲染 markdown，回退纯文本。
    """
    before = int(res.get("messages_before", 0) or 0)
    after = int(res.get("messages_after", 0) or 0)
    summary_chars = int(res.get("summary_chars", 0) or 0)
    cur_tokens = int(res.get("current_tokens", 0) or 0)
    threshold = int(res.get("threshold_tokens", 0) or 0)
    rounds = int(res.get("compression_round", 0) or 0)
    model = str(res.get("model") or "—")
    compressed = bool(res.get("compressed"))
    session_loaded = res.get("session_loaded", True)

    if not session_loaded:
        msg = "当前会话尚未在 daemon 内驻留，无法压缩。请先发送任意消息触发加载。"
        if _HAS_RICH and _RICH_CONSOLE is not None:
            _RICH_CONSOLE.print(Markdown(f"# 上下文压缩\n\n- {msg}"))
        else:
            print()
            print("  上下文压缩: " + msg)
        return

    status = "已压缩" if compressed else "未触发压缩（消息数不足以折叠或无变化）"
    md = f"""
# 上下文压缩

- **状态**: `{status}`
- **消息数**: `{before}` → `{after}`（保留 `{int(res.get("kept", after))}` 条）
- **摘要长度**: `{summary_chars}` 字符
- **触发时上下文**: `{cur_tokens:,}` token  /  阈值 `{threshold:,}` token
- **当前模型**: `{model}`
- **累计压缩轮次**: `{rounds}`
"""
    if _HAS_RICH and _RICH_CONSOLE is not None:
        _RICH_CONSOLE.print(Markdown(md))
    else:
        print()
        print("=" * 50)
        print("  上下文压缩")
        print("=" * 50)
        print(f"  状态:           {status}")
        print(f"  消息数:         {before} → {after}（保留 {int(res.get('kept', after))} 条）")
        print(f"  摘要长度:       {summary_chars} 字符")
        print(f"  触发时上下文:   {cur_tokens:,} token  /  阈值 {threshold:,} token")
        print(f"  当前模型:       {model}")
        print(f"  累计压缩轮次:   {rounds}")
        print("=" * 50)
        print()


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
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
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


def _format_remote_state_cli(state: dict[str, Any]) -> str:
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
        f"工作区: {mount} -> {path}"
    )


async def run_interactive_loop(agent: Any) -> str:
    """运行交互式对话循环，返回退出原因（quit/sigint/eof）。"""
    print_welcome()
    print(thin_separator())

    if _HAS_PROMPT_TOOLKIT:
        pt_session = _PromptSession()
        pt_prompt = HTML("<style fg='ansicyan' bold='true'>❯ </style>")
    else:
        pt_session = None
        pt_prompt = None

    prev_total_tokens = 0
    processing_task: Optional[asyncio.Task[str]] = None
    is_processing = False
    interrupted_processing = False
    show_reasoning = os.getenv("SCHEDULE_SHOW_REASONING", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }

    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _call_method(name: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(agent, name, None)
        if not callable(fn):
            return None
        return await _maybe_await(fn(*args, **kwargs))

    _DEFAULT_USAGE = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "call_count": 0,
        "cost_yuan": 0.0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
    }

    def _normalize_usage(u: dict) -> dict:
        return {**_DEFAULT_USAGE, **(u or {})}

    async def _get_token_usage() -> dict:
        usage = await _call_method("get_token_usage")
        return (
            _normalize_usage(usage) if isinstance(usage, dict) else dict(_DEFAULT_USAGE)
        )

    def _supports_session_commands() -> bool:
        return (
            hasattr(agent, "list_sessions")
            and hasattr(agent, "switch_session")
            and hasattr(agent, "active_session_id")
        )

    async def _expire_active_before_new(target_session_id: str) -> Optional[str]:
        active = getattr(agent, "active_session_id", None)
        target = (target_session_id or "").strip()
        if not active or active == target:
            return None
        expire_fn = getattr(agent, "expire_session", None)
        if not callable(expire_fn) or not inspect.iscoroutinefunction(expire_fn):
            return None
        try:
            await expire_fn(active, reason="manual_new")
        except Exception as exc:  # noqa: BLE001
            return str(exc) or type(exc).__name__
        return None

    async def _handle_session_command(raw: str) -> bool:
        if not _supports_session_commands():
            return False
        parts = raw.strip().split()
        if not parts or parts[0].lower() != "session":
            return False
        sub = parts[1].lower() if len(parts) > 1 else "show"
        if sub in {"show", "current"}:
            active = getattr(agent, "active_session_id", "unknown")
            print(hint(f"  当前会话: {active}"))
            print(thin_separator())
            return True
        if sub == "whoami":
            active = getattr(agent, "active_session_id", "unknown")
            owner = getattr(agent, "owner_id", "root")
            source = getattr(agent, "source", "cli")
            print(hint(f"  user={owner}  source={source}  session={active}"))
            print(thin_separator())
            return True
        if sub in {"list", "ls"}:
            raw_sessions = await _call_method("list_sessions")
            sessions = list(raw_sessions or [])
            active = getattr(agent, "active_session_id", "")
            print()
            if not sessions:
                print(hint("  当前没有会话。"))
            else:
                print(hint("  已加载会话:"))
                for sid in sessions:
                    marker = " *" if sid == active else ""
                    print(hint(f"    - {sid}{marker}"))
            print(thin_separator())
            return True
        if sub == "switch":
            if len(parts) < 3:
                print(hint("  用法: session switch <id>"))
                print(thin_separator())
                return True
            target = parts[2].strip()
            raw_sessions = await _call_method("list_sessions")
            sessions = list(raw_sessions or [])
            if target not in sessions:
                print(hint(f"  会话不存在: {target}"))
                print(hint("  可用 `session list` 查看，或 `session new <id>` 创建。"))
                print(thin_separator())
                return True
            await _call_method("switch_session", target, create_if_missing=False)
            print(hint(f"  已切换到会话: {target}"))
            print(thin_separator())
            return True
        if sub == "new":
            session_id = (
                parts[2].strip()
                if len(parts) > 2 and parts[2].strip()
                else f"cli:{int(time.time())}"
            )
            err = await _expire_active_before_new(session_id)
            if err:
                print(hint(f"  新建会话前保存当前会话记忆失败: {err}"))
            created = await _call_method(
                "switch_session", session_id, create_if_missing=True
            )
            if created:
                print(hint(f"  已创建并切换到新会话: {session_id}"))
            else:
                print(hint(f"  会话已存在，已切换: {session_id}"))
            print(thin_separator())
            return True
        if sub == "delete":
            if len(parts) < 3 or not parts[2].strip():
                print(hint("  用法: session delete <id>"))
                print(thin_separator())
                return True
            target = parts[2].strip()
            ok = await _call_method("delete_session", target)
            if ok:
                print(hint(f"  已删除会话记录: {target}"))
            else:
                print(hint(f"  无法删除会话: {target}（可能是当前活跃会话或不存在）"))
            print(thin_separator())
            return True
        print(
            hint(
                "  用法: session | session whoami | session list | session switch <id> | session new [id] | session delete <id>"
            )
        )
        print(thin_separator())
        return True

    prev_sigint_handler: Any = None
    sigint_handler_installed = False
    if threading.current_thread() is threading.main_thread():
        prev_sigint_handler = signal.getsignal(signal.SIGINT)

        def _sigint_handler(signum: int, frame: Any) -> None:
            nonlocal processing_task, is_processing, interrupted_processing
            if is_processing:
                interrupted_processing = True
                if processing_task is not None and not processing_task.done():
                    processing_task.cancel()
                return
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT, _sigint_handler)
        sigint_handler_installed = True

    # automation_activity.jsonl 已读到的行数，用于增量打印 [system] 消息。
    # 启动时将基准线设置为当前行数，只展示本次 CLI 会话期间新增的记录。
    automation_last_seen: int = 0
    automation_stop_event: asyncio.Event = asyncio.Event()

    base_dir_for_automation = _automation_base_dir()
    activity_path = base_dir_for_automation / "automation_activity.jsonl"
    if activity_path.exists():
        try:
            _text0 = activity_path.read_text(encoding="utf-8")
            automation_last_seen = len([ln for ln in _text0.splitlines() if ln.strip()])
        except Exception:
            automation_last_seen = 0

    def _print_pending_automation_system_messages() -> None:
        """在一次对话轮次结束后，按顺序输出尚未展示的自动化系统消息。"""
        nonlocal automation_last_seen
        base_dir = _automation_base_dir()
        path = base_dir / "automation_activity.jsonl"
        if not path.exists():
            return
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if automation_last_seen >= len(lines):
            return
        new_lines = lines[automation_last_seen:]
        automation_last_seen = len(lines)

        def _strip_markdown(s: str) -> str:
            # 粗粒度移除 markdown 标记，只保留可读文本
            for token in ("**", "__", "`", "```"):
                s = s.replace(token, "")
            return s

        for line in new_lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = rec.get("timestamp", "")
            source = rec.get("source", "")
            result = rec.get("result") or {}
            result_msg = ""
            if isinstance(result, dict):
                msg = result.get("message") or ""
                if isinstance(msg, str) and msg:
                    result_msg = _strip_markdown(msg.strip())
            prefix_ts = f"{ts} " if ts else ""
            # 只输出时间、任务名称和 Agent 最后一条消息
            if result_msg:
                text_out = f"{prefix_ts}{source} {result_msg}"
            else:
                text_out = f"{prefix_ts}{source}"
            print()
            print(label(f"[system] {text_out}"))
            print()

    async def _automation_notifier_loop() -> None:
        """后台轮询 automation_activity.jsonl 和 inject_turn push 队列，有新消息时打印。

        Agent 处理用户输入期间（is_processing=True）暂停打印，
        避免系统消息插入 spinner 或 streaming 输出中破坏 UI。
        积压的消息会在 Agent 回复完成后由主循环统一冲刷。
        """
        while not automation_stop_event.is_set():
            try:
                if not is_processing:
                    _print_pending_automation_system_messages()
                    # 轮询 subagent 完成等 inject_turn 产生的主 agent 推送回复
                    if hasattr(agent, "poll_push"):
                        try:
                            push_results = await agent.poll_push()
                            for pr in push_results:
                                output_text = (pr.get("output_text") or "").strip()
                                if not output_text:
                                    continue
                                print()
                                print(label("[agent 后台回复]"))
                                print()
                                if _HAS_RICH and _RICH_CONSOLE is not None:
                                    _RICH_CONSOLE.print(Markdown(output_text))
                                else:
                                    print(output_text)
                                print(thin_separator())
                        except Exception:
                            pass
            except Exception:
                # 不让通知异常影响主循环
                pass
            try:
                await asyncio.wait_for(
                    asyncio.shield(automation_stop_event.wait()), timeout=2.0
                )
            except asyncio.TimeoutError:
                continue

    automation_task: Optional[asyncio.Task[Any]] = asyncio.create_task(
        _automation_notifier_loop()
    )

    # patch_stdout 让所有 print() 通过 prompt_toolkit 渲染，
    # 避免后台通知直接写 stdout 破坏输入提示符的显示。
    _stdout_patcher = None
    if _HAS_PROMPT_TOOLKIT and _patch_stdout is not None:
        _stdout_patcher = _patch_stdout(raw=True)
        _stdout_patcher.__enter__()

    try:
        while True:
            try:
                if pt_session is not None and pt_prompt is not None:
                    user_input = (await pt_session.prompt_async(pt_prompt)).strip()
                else:
                    user_input = input(prompt_prefix()).strip()
            except KeyboardInterrupt:
                if interrupted_processing:
                    interrupted_processing = False
                    print()
                    print(hint("检测到中断信号，已中断当前处理。"))
                    print(thin_separator())
                    continue
                print()
                print(hint("检测到中断信号，正在退出..."))
                print()
                return "sigint"
            except asyncio.CancelledError:
                if interrupted_processing:
                    interrupted_processing = False
                    print()
                    print(hint("检测到中断信号，已中断当前处理。"))
                    print(thin_separator())
                    continue
                print()
                print(hint("检测到中断信号，正在退出..."))
                print()
                return "sigint"
            except EOFError:
                print()
                print(label("再见！"))
                print()
                return "eof"

            if not user_input:
                continue

            # 支持以 `/` 前缀显式区分指令；指令匹配时忽略这一前缀。
            raw_input = user_input
            is_slash_cmd = raw_input.startswith("/")
            cmd_text = raw_input[1:].lstrip() if is_slash_cmd else raw_input
            cmd_lower = cmd_text.lower()

            if cmd_lower in ("quit", "exit", "q"):
                u = await _get_token_usage()
                if u["call_count"] > 0:
                    print()
                    cost_str = (
                        f"，约 ¥{u['cost_yuan']:.4f}"
                        if u.get("cost_yuan") is not None
                        else ""
                    )
                    print(
                        hint(
                            f"本会话共调用 LLM {u['call_count']} 次，合计 token: {u['total_tokens']}（输入 {u['prompt_tokens']} + 输出 {u['completion_tokens']}）{cost_str}"
                        )
                    )
                print()
                print(label("再见！"))
                print()
                return "quit"

            if cmd_lower == "clear":
                await _call_method("clear_context")
                print(hint("  对话历史已清空。"))
                print(thin_separator())
                continue

            if cmd_lower.split()[:1] == ["compress"]:
                parts_c = cmd_text.strip().split()
                keep_arg: Optional[int] = None
                if len(parts_c) >= 2:
                    try:
                        keep_arg = max(1, int(parts_c[1]))
                    except (TypeError, ValueError):
                        print(
                            hint(
                                "  用法: /compress [N]   N 为保留最近几轮（正整数）"
                            )
                        )
                        print(thin_separator())
                        continue
                fn = getattr(agent, "compress_context", None)
                if not callable(fn):
                    print(hint("  当前 agent 不支持 /compress 指令。"))
                    print(thin_separator())
                    continue
                try:
                    res = await _maybe_await(fn(keep_recent_turns=keep_arg))
                except Exception as exc:
                    print(accent("  压缩失败: ") + str(exc))
                    print(thin_separator())
                    continue
                _print_compress_result(res or {})
                print(thin_separator())
                continue

            if cmd_lower == "goal" or cmd_text.lower().startswith("goal "):
                parts_g = cmd_text.strip().split(maxsplit=1)
                sub_g = parts_g[1].strip() if len(parts_g) > 1 else ""
                sub_gl = sub_g.lower()
                if not sub_g or sub_gl in ("help", "-h", "?"):
                    print(hint("  /goal <instruction>  创建目标并开始执行"))
                    print(hint("  /goal list           列出活跃目标"))
                    print(thin_separator())
                    continue
                create_fn = getattr(agent, "create_goal", None)
                list_fn = getattr(agent, "list_goals", None)
                if not callable(create_fn):
                    print(hint("  当前 agent 不支持 /goal（需连接 daemon IPC）。"))
                    print(thin_separator())
                    continue
                if sub_gl in ("list", "ls"):
                    if not callable(list_fn):
                        print(hint("  当前 agent 不支持 /goal list。"))
                        print(thin_separator())
                        continue
                    try:
                        res = await _maybe_await(list_fn())
                    except Exception as exc:
                        print(accent("  列出目标失败: ") + str(exc))
                        print(thin_separator())
                        continue
                    goals = (res or {}).get("goals") if isinstance(res, dict) else []
                    if not goals:
                        print(hint("  当前没有活跃的 Agent 目标。"))
                    else:
                        print(hint("  当前 Agent 目标："))
                        for g in goals:
                            if not isinstance(g, dict):
                                continue
                            print(
                                hint(
                                    f"    - {g.get('id', '—')} [{g.get('status', '?')}] "
                                    f"{g.get('title', '')}"
                                )
                            )
                    print(thin_separator())
                    continue
                try:
                    res = await _maybe_await(create_fn(sub_g, autostart=True))
                except Exception as exc:
                    print(accent("  创建目标失败: ") + str(exc))
                    print(thin_separator())
                    continue
                if isinstance(res, dict) and res.get("goal"):
                    g = res["goal"]
                    print(hint(f"  已创建目标 {g.get('id', '—')}: {g.get('title', '')}"))
                    if res.get("autostart_queued"):
                        print(hint("  Agent 已开始执行此目标。"))
                else:
                    print(hint("  目标已创建。"))
                print(thin_separator())
                continue

            if cmd_lower == "help":
                print_help()
                print(thin_separator())
                continue

            if cmd_lower in ("usage", "stats", "tokens"):
                print_token_usage_data(await _get_token_usage())
                print(thin_separator())
                continue

            if await _handle_session_command(cmd_text):
                continue

            # ── /interrupt | /cancel | /stop ─────────────────────────────
            if cmd_lower in ("interrupt", "cancel", "stop"):
                sid = str(getattr(agent, "active_session_id", "") or "").strip()
                if not sid:
                    print(hint("  无法解析当前会话 ID，请先发一条普通消息再试。"))
                    print(thin_separator())
                    continue
                try:
                    cancelled = await _call_method("terminal_cancel", sid)
                except Exception as exc:
                    print(accent(f"  中断失败: {exc}"))
                    print(thin_separator())
                    continue
                if cancelled is None:
                    print(hint("  当前 agent 不支持中断指令。"))
                elif cancelled:
                    print(hint("  Chat session interrupted."))
                else:
                    print(hint("  No active chat session."))
                print(thin_separator())
                continue

            # ── /remote-use | /remote-status | /remote-release ───────────
            if cmd_lower in ("remote-status", "remote status"):
                try:
                    res = await _call_method("remote_workspace_status")
                except Exception as exc:
                    print(accent(f"  远程工作区状态读取失败: {exc}"))
                    print(thin_separator())
                    continue
                if res is None:
                    print(hint("  当前 agent 不支持远程工作区指令。"))
                else:
                    state = res.get("state") if isinstance(res, dict) else None
                    print(hint(_format_remote_state_cli(state if isinstance(state, dict) else {})))
                print(thin_separator())
                continue

            if cmd_lower in ("remote-release", "remote release", "cloud-use"):
                try:
                    res = await _call_method("remote_workspace_release")
                except Exception as exc:
                    print(accent(f"  释放远程工作区失败: {exc}"))
                    print(thin_separator())
                    continue
                if res is None:
                    print(hint("  当前 agent 不支持远程工作区指令。"))
                else:
                    released = bool(res.get("released")) if isinstance(res, dict) else False
                    if released:
                        print(hint("  已释放远程工作区，当前会话将恢复云端工作区模式。"))
                    else:
                        print(hint("  当前会话未启用远程工作区。"))
                print(thin_separator())
                continue

            if cmd_lower.startswith("remote-use"):
                parsed, err = _parse_remote_use_args(cmd_text)
                if err:
                    print(hint(f"  {err}"))
                    print(thin_separator())
                    continue
                try:
                    state = await _call_method("remote_workspace_use", **parsed)
                except Exception as exc:
                    print(accent(f"  切换远程工作区失败: {exc}"))
                    print(thin_separator())
                    continue
                if state is None:
                    print(hint("  当前 agent 不支持远程工作区指令。"))
                else:
                    print(hint(_format_remote_state_cli(state if isinstance(state, dict) else {})))
                print(thin_separator())
                continue

            # ── /dangerously ────────────────────────────────────────────
            if cmd_lower.split()[:1] in (["dangerously"], ["danger"], ["dangerous"]):
                parts_d = cmd_text.strip().split()
                action = parts_d[1].strip().lower() if len(parts_d) > 1 else "status"
                if action not in {"on", "off", "status"}:
                    print(hint("  用法: /dangerously on|off|status"))
                    print(thin_separator())
                    continue
                try:
                    if action == "status":
                        status = await _call_method("get_dangerous_mode")
                    else:
                        status = await _call_method("set_dangerous_mode", enabled=(action == "on"))
                except Exception as exc:
                    print(accent(f"  查询/切换失败: {exc}"))
                    print(thin_separator())
                    continue
                if not isinstance(status, dict):
                    print(hint("  当前 agent 不支持 dangerous mode 指令。"))
                    print(thin_separator())
                    continue
                enabled_now = bool(status.get("dangerous_mode_enabled"))
                sid = str(status.get("session_id") or getattr(agent, "active_session_id", ""))
                if enabled_now:
                    msg = (
                        "Dangerous mode is ENABLED.\n"
                        "Human approval is bypassed for permission checks in this session.\n"
                        f"Session: {sid}"
                    )
                else:
                    msg = (
                        "Dangerous mode is DISABLED.\n"
                        "Human approval is required again.\n"
                        f"Session: {sid}"
                    )
                print(hint(f"  {msg}"))
                print(thin_separator())
                continue

            # ── /new [id] ───────────────────────────────────────────────
            if cmd_lower.split()[:1] == ["new"]:
                parts_n = cmd_text.strip().split()
                session_id = (
                    parts_n[1].strip()
                    if len(parts_n) > 1 and parts_n[1].strip()
                    else f"cli:{int(time.time())}"
                )
                err = await _expire_active_before_new(session_id)
                if err:
                    print(hint(f"  新建会话前保存当前会话记忆失败: {err}"))
                created = await _call_method(
                    "switch_session", session_id, create_if_missing=True
                )
                if created:
                    print(hint(f"  已创建并切换到新会话: {session_id}"))
                else:
                    print(hint(f"  会话已存在，已切换: {session_id}"))
                print(thin_separator())
                continue

            if cmd_lower.startswith("model"):
                parts = cmd_text.strip().split(maxsplit=1)
                sub = parts[1].strip() if len(parts) > 1 else ""
                sub_tokens = sub.split()
                if sub_tokens and sub_tokens[0].lower() == "switch":
                    sub = " ".join(sub_tokens[1:]).strip()
                sub_lower = sub.lower()
                list_fn = getattr(agent, "list_models", None)
                switch_fn = getattr(agent, "switch_model", None)
                if not callable(list_fn) or not callable(switch_fn):
                    print(hint("  当前 agent 不支持 /model 指令。"))
                    print(thin_separator())
                    continue
                if not sub or sub_lower in ("list", "ls"):
                    try:
                        models = await _maybe_await(list_fn())
                    except Exception as exc:
                        print(accent("  列出模型失败: ") + str(exc))
                        print(thin_separator())
                        continue
                    print()
                    if not models:
                        print(hint("  未找到可用 provider。"))
                    else:
                        print(
                            hint(
                                    "* = Current active model  |  V = vision_provider"
                                )
                        )
                        print(
                            hint(
                                "Usage: /model <model name> (e.g., /model Qwen3.5 Plus)"
                            )
                        )
                        print(
                            hint(
                                "       /model add help  — register new base_url/api_key/model at runtime"
                            )
                        )
                        print()
                        h_show = "Model Name"
                        h_cap = "Capabilities"
                        print(
                            hint(
                                f"    {'':2}{'':2}  {h_show:<42} {h_cap}"
                            )
                        )
                        for m in models:
                            mark = " *" if m.get("is_active") else "  "
                            vp = " V" if m.get("is_vision_provider") else "  "
                            raw_label = m.get("label")
                            display = (
                                str(raw_label).strip()
                                if raw_label not in (None, "")
                                else str(m.get("name") or "—")
                            )
                            caps_bits = []
                            if m.get("vision"):
                                caps_bits.append("vision")
                            if m.get("function_calling"):
                                caps_bits.append("tools")
                            cap_s = ",".join(caps_bits) if caps_bits else "—"
                            print(
                                hint(
                                    f"   {mark}{vp}  {display:<42} {cap_s}"
                                )
                            )
                    print(thin_separator())
                    continue
                target = sub
                try:
                    info = await _maybe_await(switch_fn(target))
                except Exception as exc:
                    print(accent(f"  切换模型失败: ") + str(exc))
                    print(thin_separator())
                    continue
                if isinstance(info, dict) and info.get("name"):
                    vision_flag = "支持视觉" if info.get("vision") else "无视觉"
                    vp = info.get("vision_provider") or "-"
                    api_id = info.get("api_model") or info.get("model")
                    print(
                        hint(
                            "  已切换主对话 provider: "
                            f"{info.get('name')}  |  API 模型 ID: {api_id}  |  {vision_flag}  |  vision_provider={vp}"
                        )
                    )
                else:
                    print(hint(f"  已请求切换至: {target}"))
                print(thin_separator())
                continue

            # ── 处理用户输入 ──
            spinner_stop: Optional[asyncio.Event] = None
            spinner_task: Optional[asyncio.Task[Any]] = None
            stream_started = False
            stream_buffer = ""
            live: Any = None
            last_render_ts = 0.0
            reasoning_started = False
            reasoning_buffer = ""
            io_lock = threading.Lock()

            try:
                spinner_stop = asyncio.Event()
                width = shutil.get_terminal_size((80, 20)).columns
                spinner_line_active = False
                spinner_paused = False
                last_text_output_ts = time.monotonic()
                _spinner_stop = spinner_stop

                # ── Spinner ──
                async def _run_spinner() -> None:
                    nonlocal spinner_line_active
                    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
                    i = 0
                    while not _spinner_stop.is_set():
                        if spinner_paused or (
                            time.monotonic() - last_text_output_ts < 0.35
                        ):
                            with io_lock:
                                if spinner_line_active:
                                    _erase_spinner_line()
                            await asyncio.sleep(0.03)
                            continue
                        with io_lock:
                            if spinner_paused:
                                if spinner_line_active:
                                    _erase_spinner_line()
                                continue
                            sys.stdout.write("\r" + frames[i % len(frames)])
                            sys.stdout.flush()
                            spinner_line_active = True
                        i += 1
                        await asyncio.sleep(0.1)
                    with io_lock:
                        if spinner_line_active:
                            sys.stdout.write("\r" + " " * width + "\r")
                            sys.stdout.flush()
                            spinner_line_active = False

                spinner_task = asyncio.create_task(_run_spinner())

                def _erase_spinner_line() -> None:
                    nonlocal spinner_line_active
                    if spinner_line_active:
                        sys.stdout.write("\r" + " " * width + "\r")
                        sys.stdout.flush()
                        spinner_line_active = False

                def _pause_spinner() -> None:
                    nonlocal spinner_paused
                    spinner_paused = True

                def _resume_spinner() -> None:
                    nonlocal spinner_paused
                    if spinner_stop is not None and not spinner_stop.is_set():
                        spinner_paused = False

                def _stop_spinner() -> None:
                    _pause_spinner()
                    with io_lock:
                        _erase_spinner_line()
                    if spinner_stop is not None and not spinner_stop.is_set():
                        spinner_stop.set()

                def _print_with_spinner(text: str = "", end: str = "\n") -> None:
                    nonlocal last_text_output_ts
                    _pause_spinner()
                    with io_lock:
                        _erase_spinner_line()
                        print(text, end=end)
                        sys.stdout.flush()
                    last_text_output_ts = time.monotonic()
                    _resume_spinner()

                def _short(obj: object, max_len: int = 120) -> str:
                    try:
                        text = (
                            obj
                            if isinstance(obj, str)
                            else json.dumps(obj, ensure_ascii=False, default=str)
                        )
                    except Exception:
                        text = str(obj)
                    if len(text) <= max_len:
                        return text
                    return text[: max_len - 3] + "..."

                # ── Live 块管理 ──

                def _persist_live_block(final_content: Optional[str] = None) -> None:
                    """将当前 Live 块持久化（内容留在终端）并重置状态。

                    Args:
                        final_content: 若提供，用它做最后一次渲染（确保完整）。
                    """
                    nonlocal live, stream_started, stream_buffer, last_render_ts
                    if live is not None:
                        content = final_content or stream_buffer
                        if content.strip():
                            live.update(Markdown(content), refresh=True)
                        live.transient = False
                        try:
                            live.__exit__(None, None, None)
                        except Exception:
                            pass
                        live = None
                    stream_started = False
                    stream_buffer = ""
                    last_render_ts = 0.0

                def _flush_reasoning_buffer() -> None:
                    nonlocal reasoning_buffer, reasoning_started
                    text = reasoning_buffer.strip()
                    reasoning_buffer = ""
                    if text:
                        _print_with_spinner(t(text, dim=True))
                        reasoning_started = True

                # ── 流式回调 ──

                def on_stream_delta(delta: str) -> None:
                    """每段 LLM 输出都是正式回复，Rich Live Markdown 流式渲染。"""
                    nonlocal \
                        stream_started, \
                        stream_buffer, \
                        live, \
                        last_render_ts, \
                        last_text_output_ts
                    if not delta:
                        return
                    stream_buffer += delta

                    if not stream_started:
                        _pause_spinner()
                        with io_lock:
                            _erase_spinner_line()
                        _flush_reasoning_buffer()
                        # flush 内部的 _print_with_spinner 会 resume spinner，
                        # 必须重新 pause，否则 spinner 会在整个 Live 期间和 Rich 抢 stdout
                        _pause_spinner()
                        with io_lock:
                            _erase_spinner_line()
                        stream_started = True
                        print()
                        if _HAS_RICH and _RICH_CONSOLE is not None:
                            live = Live(
                                Markdown(""),
                                console=_RICH_CONSOLE,
                                refresh_per_second=12,
                                transient=True,
                            )
                            live.__enter__()

                    if live is not None:
                        now = time.monotonic()
                        if now - last_render_ts >= 0.08:
                            live.update(Markdown(stream_buffer), refresh=True)
                            last_render_ts = now
                            last_text_output_ts = now
                    else:
                        sys.stdout.write(delta)
                        sys.stdout.flush()
                        last_text_output_ts = time.monotonic()

                def on_reasoning_delta(delta: str) -> None:
                    """思维链：dim 文本逐行流式输出"""
                    nonlocal reasoning_buffer
                    if not show_reasoning:
                        return
                    if not delta or stream_started:
                        return
                    reasoning_buffer += delta
                    while "\n" in reasoning_buffer:
                        line, reasoning_buffer = reasoning_buffer.split("\n", 1)
                        if line:
                            _print_with_spinner(t(line, dim=True))
                    if len(reasoning_buffer) > 200:
                        _flush_reasoning_buffer()

                def on_trace_event(event: dict) -> None:
                    nonlocal reasoning_started, last_render_ts
                    event_type = event.get("type")
                    if event_type == "llm_request":
                        _flush_reasoning_buffer()
                        if reasoning_started:
                            _print_with_spinner()
                        reasoning_started = False
                        if stream_started:
                            _persist_live_block()
                        last_render_ts = 0.0
                        _resume_spinner()
                        iteration = event.get("iteration")
                        tool_count = event.get("tool_count")
                        _print_with_spinner()
                        _print_with_spinner(
                            hint(
                                f"  第 {iteration} 步: 调用模型（可用工具 {tool_count}）"
                            )
                        )
                    elif event_type == "tool_call":
                        _flush_reasoning_buffer()
                        if stream_started:
                            _persist_live_block()
                            _resume_spinner()
                        name = event.get("name")
                        args = _short(event.get("arguments", {}))
                        _print_with_spinner(hint(f"  → 调用工具: {name}({args})"))
                    elif event_type == "tool_result":
                        name = event.get("name")
                        ok = "成功" if event.get("success") else "失败"
                        msg = _short(event.get("message", ""))
                        ms = event.get("duration_ms")
                        _print_with_spinner(
                            hint(f"  → 工具结果: {name} {ok}（{ms}ms） {msg}")
                        )
                    elif event_type == "chat_history_summarized":
                        _flush_reasoning_buffer()
                        if stream_started:
                            _persist_live_block()
                        _resume_spinner()
                        note = str(event.get("message") or "").strip() or (
                            "Chat History Summarized."
                        )
                        _print_with_spinner(hint(f"  {note}"))

                # ── ask_user / permission 终端交互 ──────────────────────────
                # run_turn_stream 会在 tool trace 之后顺序投递这些事件；
                # 此时主线程正在 await processing_task，可以在回调里同步读 stdin。
                # 为避免 stdin 竞争，使用 prompt_toolkit 若可用，否则回退 input()。
                def _prompt_sync(prompt_text: str) -> str:
                    if _HAS_PROMPT_TOOLKIT and pt_session is not None:
                        try:
                            loop = asyncio.get_running_loop()
                            return loop.run_until_complete(
                                pt_session.prompt_async(HTML(prompt_text))
                            )
                        except Exception:
                            pass
                    return input(prompt_text)

                def on_feishu_ask_user_notify(
                    batch_id: str, payload: dict[str, Any]
                ) -> None:
                    questions = payload.get("questions") or []
                    if not isinstance(questions, list) or not questions:
                        return
                    _stop_spinner()
                    _flush_reasoning_buffer()
                    if stream_started:
                        _persist_live_block()
                    _pause_spinner()
                    with io_lock:
                        _erase_spinner_line()
                    print()
                    print(label("[Agent 提问]"))
                    print()
                    answers_out: list[dict[str, Any]] = []
                    for qi, q in enumerate(questions):
                        qid = str(q.get("id") or f"q{qi + 1}")
                        text = str(q.get("text") or q.get("question") or "").strip()
                        options = q.get("options") or []
                        allow_custom = bool(q.get("allow_custom", True))
                        print(hint(f"  Q{qi + 1}: {text}"))
                        if isinstance(options, list) and options:
                            for oi, opt in enumerate(options):
                                opt_label = str(opt.get("label") or opt.get("value") or f"{oi}")
                                opt_text = str(opt.get("text") or opt.get("label") or "")
                                print(hint(f"    [{oi}] {opt_label}") + (
                                    f" - {opt_text}" if opt_text and opt_text != opt_label else ""
                                ))
                            opt_range = f"0-{len(options) - 1}"
                        else:
                            opt_range = "无选项"
                        custom_hint = " 或直接输入回答" if allow_custom else ""
                        prompt_line = f"  请选择 [{opt_range}]{custom_hint} (q=跳过): "
                        while True:
                            try:
                                choice = _prompt_sync(prompt_line).strip()
                            except (EOFError, KeyboardInterrupt):
                                choice = "q"
                            if choice.lower() == "q":
                                answers_out.append({
                                    "question_id": qid,
                                    "selected_option": None,
                                    "custom_text": None,
                                })
                                break
                            if isinstance(options, list) and options:
                                try:
                                    idx = int(choice)
                                    if 0 <= idx < len(options):
                                        sel = options[idx]
                                        val = str(sel.get("value") or sel.get("label") or f"{idx}")
                                        answers_out.append({
                                            "question_id": qid,
                                            "selected_option": val,
                                            "custom_text": None,
                                        })
                                        break
                                except (TypeError, ValueError):
                                    pass
                            if allow_custom and choice:
                                answers_out.append({
                                    "question_id": qid,
                                    "selected_option": None,
                                    "custom_text": choice,
                                })
                                break
                            print(hint("    无效输入，请重试。"))
                        print()
                    print(thin_separator())
                    _resume_spinner()
                    # 异步提交答案到 IPC（不阻塞回调返回）
                    fn = getattr(agent, "resolve_ask_user", None)
                    if callable(fn) and inspect.iscoroutinefunction(fn):
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(fn(batch_id=batch_id, answers=answers_out))
                        except Exception:
                            pass

                def on_feishu_permission_notify(
                    permission_id: str, payload: dict[str, Any]
                ) -> None:
                    summary = str(payload.get("summary") or "").strip()
                    kind = str(payload.get("kind") or "").strip()
                    path_prefix = str(payload.get("path_prefix") or "").strip()
                    auto_exec = bool(payload.get("auto_execute_after_approval"))
                    _stop_spinner()
                    _flush_reasoning_buffer()
                    if stream_started:
                        _persist_live_block()
                    _pause_spinner()
                    with io_lock:
                        _erase_spinner_line()
                    print()
                    print(label("[权限请求]"))
                    if summary:
                        print(hint(f"  {summary}"))
                    if kind:
                        print(hint(f"  类型: {kind}"))
                    if path_prefix:
                        print(hint(f"  路径前缀: {path_prefix}"))
                    if auto_exec:
                        print(hint("  批准后将自动继续执行原操作。"))
                    print(hint(f"  permission_id: {permission_id}"))
                    print()
                    prompt_line = "  批准? [y/n/c=需要澄清/q=跳过]: "
                    while True:
                        try:
                            choice = _prompt_sync(prompt_line).strip().lower()
                        except (EOFError, KeyboardInterrupt):
                            choice = "q"
                        if choice == "q":
                            print(thin_separator())
                            _resume_spinner()
                            return
                        if choice == "c":
                            note = ""
                            try:
                                note = _prompt_sync("  请说明需要澄清的内容: ").strip()
                            except (EOFError, KeyboardInterrupt):
                                pass
                            print(thin_separator())
                            _resume_spinner()
                            fn = getattr(agent, "resolve_permission", None)
                            if callable(fn) and inspect.iscoroutinefunction(fn):
                                try:
                                    loop = asyncio.get_running_loop()
                                    loop.create_task(
                                        fn(
                                            permission_id=permission_id,
                                            allowed=False,
                                            clarify_requested=True,
                                            user_instruction=note or None,
                                        )
                                    )
                                except Exception:
                                    pass
                            return
                        if choice in ("y", "yes"):
                            persist = False
                            try:
                                persist_in = _prompt_sync(
                                    "  是否持久化该路径授权? [y/N]: "
                                ).strip().lower()
                                persist = persist_in in ("y", "yes")
                            except (EOFError, KeyboardInterrupt):
                                pass
                            print(thin_separator())
                            _resume_spinner()
                            fn = getattr(agent, "resolve_permission", None)
                            if callable(fn) and inspect.iscoroutinefunction(fn):
                                try:
                                    loop = asyncio.get_running_loop()
                                    loop.create_task(
                                        fn(
                                            permission_id=permission_id,
                                            allowed=True,
                                            persist_acl=persist,
                                        )
                                    )
                                except Exception:
                                    pass
                            return
                        if choice in ("n", "no"):
                            print(thin_separator())
                            _resume_spinner()
                            fn = getattr(agent, "resolve_permission", None)
                            if callable(fn) and inspect.iscoroutinefunction(fn):
                                try:
                                    loop = asyncio.get_running_loop()
                                    loop.create_task(
                                        fn(
                                            permission_id=permission_id,
                                            allowed=False,
                                        )
                                    )
                                except Exception:
                                    pass
                            return
                        print(hint("    无效输入，请重试。"))

                is_processing = True
                hooks = AgentHooks(
                    on_assistant_delta=on_stream_delta,
                    on_reasoning_delta=on_reasoning_delta,
                    on_trace_event=on_trace_event,
                    on_feishu_ask_user_notify=on_feishu_ask_user_notify,
                    on_feishu_permission_notify=on_feishu_permission_notify,
                )
                processing_task = asyncio.create_task(
                    agent.run_turn(AgentRunInput(text=user_input), hooks=hooks)
                )
                _raw_result = await processing_task
                resp_text = getattr(_raw_result, "output_text", None)
                response = (
                    resp_text if isinstance(resp_text, str) else str(_raw_result)
                )
                _stop_spinner()
                _flush_reasoning_buffer()
                if spinner_task is not None:
                    await spinner_task

                # ── 最终回复渲染 ──
                if live is not None:
                    _persist_live_block(response)
                    print()
                else:
                    print()
                    if _HAS_RICH and _RICH_CONSOLE is not None:
                        _RICH_CONSOLE.print(Markdown(response))
                    else:
                        print(response)
                    print()

                u = await _get_token_usage()
                delta = u["total_tokens"] - prev_total_tokens
                prev_total_tokens = u["total_tokens"]
                cost = u.get("cost_yuan")
                print(status_bar(u["total_tokens"], u["call_count"], delta, cost))
                # 在本轮对话完全结束后，按顺序输出后台自动化的 [system] 消息
                _print_pending_automation_system_messages()

            except (KeyboardInterrupt, asyncio.CancelledError):
                interrupted_processing = False
                if spinner_stop is not None:
                    spinner_stop.set()
                with io_lock:
                    sys.stdout.write(
                        "\r" + " " * shutil.get_terminal_size((80, 20)).columns + "\r"
                    )
                    sys.stdout.flush()
                if spinner_task is not None:
                    try:
                        await spinner_task
                    except Exception:
                        pass
                if live is not None:
                    try:
                        live.__exit__(None, None, None)
                    except Exception:
                        pass
                    live = None

                print()
                print(hint("检测到中断信号，已中断当前处理。"))
                print(thin_separator())
                continue
            except Exception as e:
                interrupted_processing = False
                if spinner_stop is not None:
                    spinner_stop.set()
                with io_lock:
                    sys.stdout.write(
                        "\r" + " " * shutil.get_terminal_size((80, 20)).columns + "\r"
                    )
                    sys.stdout.flush()
                if spinner_task is not None:
                    try:
                        await spinner_task
                    except Exception:
                        pass
                if live is not None:
                    try:
                        live.__exit__(None, None, None)
                    except Exception:
                        pass
                    live = None

                print()
                print(accent("  抱歉，处理您的请求时发生错误: ") + str(e))
                print(hint("  请重试或换一种方式表达。"))
                print(thin_separator())
            finally:
                is_processing = False
                processing_task = None

    finally:
        if _stdout_patcher is not None:
            try:
                _stdout_patcher.__exit__(None, None, None)
            except Exception:
                pass
        if sigint_handler_installed:
            signal.signal(signal.SIGINT, prev_sigint_handler)
        if automation_task is not None:
            automation_stop_event.set()
            try:
                await automation_task
            except Exception:
                pass
