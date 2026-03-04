"""
CLI 交互式界面

包含欢迎信息、帮助、token 用量展示以及主交互循环。
"""

import asyncio
import inspect
import json
import signal
import sys
import shutil
import threading
import time
from typing import Any, Optional

from schedule_agent.core import ScheduleAgent
from schedule_agent.core.interfaces import AgentHooks, AgentRunInput
from schedule_agent.automation.repositories import _automation_base_dir
from schedule_agent.utils.cli_style import (
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


async def _thinking_spinner(stop_event: asyncio.Event) -> None:
    """简单的「正在思考」动画（备用，主循环有内置版本）"""
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    i = 0
    width = shutil.get_terminal_size((80, 20)).columns
    while not stop_event.is_set():
        sys.stdout.write("\r" + frames[i % len(frames)])
        sys.stdout.flush()
        i += 1
        try:
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            break
    sys.stdout.write("\r" + " " * width + "\r")
    sys.stdout.flush()


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
        print("  Schedule Agent - 智能日程管理助手")
        print("=" * 50)
        print()
        print("你好！我是你的日程管理助手，可以帮助你：")
        print("  • 添加日程事件（会议、约会等）")
        print("  • 创建待办任务")
        print("  • 查询日程和任务")
        print("  • 智能规划时间")
        print()
        print("命令： quit/exit 退出  |  clear 清空对话  |  help 帮助  |  usage/stats 用量")
        print("-" * 50)
        print()


def print_help():
    """打印帮助信息"""
    md = """
# 帮助信息

## 可用命令

- `quit` / `exit` &nbsp;&nbsp;退出程序
- `clear` &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;清空对话历史
- `help` &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;显示此帮助
- `usage` / `stats` &nbsp;&nbsp;本会话 token 用量
- `session` &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;显示当前会话
- `session whoami` &nbsp;显示当前 user/source/session
- `session list` &nbsp;列出当前已加载会话
- `session switch <id>` &nbsp;切换到指定会话（已存在）
- `session new [id]` &nbsp;创建并切换到新会话

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
        print("  quit / exit  退出程序")
        print("  clear       清空对话历史")
        print("  help        显示此帮助")
        print("  usage/stats 本会话 token 用量")
        print("  session     查看当前会话")
        print("  session whoami")
        print("  session list")
        print("  session switch <id>")
        print("  session new [id]")
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
    """打印本会话 token 用量统计"""
    cost_line = f"\n- **预估费用**: `¥{u['cost_yuan']:.4f}`" if u.get("cost_yuan") is not None else ""
    md = f"""
# Token 用量统计

- **调用次数**: `{u['call_count']}`
- **输入 token**: `{u['prompt_tokens']}`
- **输出 token**: `{u['completion_tokens']}`
- **合计 token**: `{u['total_tokens']}`{cost_line}
"""
    if _HAS_RICH and _RICH_CONSOLE is not None:
        _RICH_CONSOLE.print(Markdown(md))
    else:
        print()
        print("=" * 50)
        print("  本会话 Token 用量统计")
        print("=" * 50)
        print(f"  调用次数:     {u['call_count']}")
        print(f"  输入 token:   {u['prompt_tokens']}")
        print(f"  输出 token:   {u['completion_tokens']}")
        print(f"  合计 token:   {u['total_tokens']}")
        if u.get("cost_yuan") is not None:
            print(f"  预估费用:     ¥{u['cost_yuan']:.4f}")
        print("=" * 50)
        print()


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

    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _call_method(name: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(agent, name, None)
        if not callable(fn):
            return None
        return await _maybe_await(fn(*args, **kwargs))

    async def _get_token_usage() -> dict:
        usage = await _call_method("get_token_usage")
        if isinstance(usage, dict):
            return usage
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "call_count": 0, "cost_yuan": 0.0}

    def _supports_session_commands() -> bool:
        return (
            hasattr(agent, "list_sessions")
            and hasattr(agent, "switch_session")
            and hasattr(agent, "active_session_id")
        )

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
            session_id = parts[2].strip() if len(parts) > 2 and parts[2].strip() else f"cli:{int(time.time())}"
            created = await _call_method("switch_session", session_id, create_if_missing=True)
            if created:
                print(hint(f"  已创建并切换到新会话: {session_id}"))
            else:
                print(hint(f"  会话已存在，已切换: {session_id}"))
            print(thin_separator())
            return True
        print(hint("  用法: session | session whoami | session list | session switch <id> | session new [id]"))
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
            automation_last_seen = len(
                [ln for ln in _text0.splitlines() if ln.strip()]
            )
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
        new_lines = lines[automation_last_seen : ]
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
        """后台轮询 automation_activity.jsonl，有新记录时打印 [system] 消息。
        
        Agent 处理用户输入期间（is_processing=True）暂停打印，
        避免系统消息插入 spinner 或 streaming 输出中破坏 UI。
        积压的消息会在 Agent 回复完成后由主循环统一冲刷。
        """
        while not automation_stop_event.is_set():
            try:
                if not is_processing:
                    _print_pending_automation_system_messages()
            except Exception:
                # 不让通知异常影响主循环
                pass
            try:
                await asyncio.wait_for(asyncio.shield(automation_stop_event.wait()), timeout=5.0)
            except asyncio.TimeoutError:
                continue

    automation_task: Optional[asyncio.Task[Any]] = asyncio.create_task(_automation_notifier_loop())
    session_cut_task: Optional[asyncio.Task[Any]] = None

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
                    user_input = (
                        await pt_session.prompt_async(pt_prompt)
                    ).strip()
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

            if user_input.lower() in ("quit", "exit", "q"):
                u = await _get_token_usage()
                if u["call_count"] > 0:
                    print()
                    cost_str = f"，约 ¥{u['cost_yuan']:.4f}" if u.get("cost_yuan") is not None else ""
                    print(hint(f"本会话共调用 LLM {u['call_count']} 次，合计 token: {u['total_tokens']}（输入 {u['prompt_tokens']} + 输出 {u['completion_tokens']}）{cost_str}"))
                print()
                print(label("再见！"))
                print()
                return "quit"

            if user_input.lower() == "clear":
                await _call_method("clear_context")
                print(hint("  对话历史已清空。"))
                print(thin_separator())
                continue

            if user_input.lower() == "help":
                print_help()
                print(thin_separator())
                continue

            if user_input.lower() in ("usage", "stats", "tokens"):
                print_token_usage_data(await _get_token_usage())
                print(thin_separator())
                continue

            if await _handle_session_command(user_input):
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
                        if spinner_paused or (time.monotonic() - last_text_output_ts < 0.35):
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
                    nonlocal stream_started, stream_buffer, live, last_render_ts, last_text_output_ts
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
                        _print_with_spinner(hint(f"  第 {iteration} 步: 调用模型（可用工具 {tool_count}）"))
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
                        _print_with_spinner(hint(f"  → 工具结果: {name} {ok}（{ms}ms） {msg}"))

                is_processing = True
                if hasattr(agent, "run_turn"):
                    hooks = AgentHooks(
                        on_assistant_delta=on_stream_delta,
                        on_reasoning_delta=on_reasoning_delta,
                        on_trace_event=on_trace_event,
                    )
                    processing_task = asyncio.create_task(
                        agent.run_turn(AgentRunInput(text=user_input), hooks=hooks)
                    )
                    _raw_result = await processing_task
                    response = _raw_result.output_text if hasattr(_raw_result, "output_text") else str(_raw_result)
                else:
                    processing_task = asyncio.create_task(
                        agent.process_input(
                            user_input,
                            on_stream_delta=on_stream_delta,
                            on_reasoning_delta=on_reasoning_delta,
                            on_trace_event=on_trace_event,
                        )
                    )
                    response = await processing_task
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
                    sys.stdout.write("\r" + " " * shutil.get_terminal_size((80, 20)).columns + "\r")
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
                    sys.stdout.write("\r" + " " * shutil.get_terminal_size((80, 20)).columns + "\r")
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
        if session_cut_task is not None:
            try:
                await session_cut_task
            except Exception:
                pass
