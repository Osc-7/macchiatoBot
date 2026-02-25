"""
CLI 交互式界面

包含欢迎信息、帮助、token 用量展示以及主交互循环。
"""

import asyncio
import json
import sys
import shutil
import threading
import time
from typing import Any, Optional

from schedule_agent.core import ScheduleAgent
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
try:
    from prompt_toolkit import PromptSession as _PromptSession
    from prompt_toolkit.formatted_text import HTML
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
        print()
        print("示例对话:")
        print("  • 明天下午3点有个团队会议")
        print("  • 添加一个任务：完成项目报告，预计2小时，周五前完成")
        print("  • 查看今天的日程")
        print("  • 查看我的待办任务")
        print("  • 帮我规划一下明天的任务")
        print("-" * 50)
        print()


def print_token_usage(agent: ScheduleAgent):
    """打印本会话 token 用量统计"""
    u = agent.get_token_usage()
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


async def run_interactive_loop(agent: ScheduleAgent):
    """运行交互式对话循环。"""
    print_welcome()
    print(thin_separator())

    if _HAS_PROMPT_TOOLKIT:
        pt_session = _PromptSession()
        pt_prompt = HTML("<style fg='ansicyan' bold='true'>❯ </style>")
    else:
        pt_session = None
        pt_prompt = None

    prev_total_tokens = 0
    ui_cfg = getattr(getattr(agent, "config", None), "ui", None)
    show_draft = str(getattr(ui_cfg, "show_draft", "summary") or "summary").lower()
    if show_draft not in {"off", "summary", "full"}:
        show_draft = "summary"
    draft_max_chars = int(getattr(ui_cfg, "draft_max_chars", 500) or 500)

    while True:
        try:
            if pt_session is not None and pt_prompt is not None:
                user_input = (
                    await pt_session.prompt_async(pt_prompt)
                ).strip()
            else:
                user_input = input(prompt_prefix()).strip()

            if not user_input:
                continue

            if user_input.lower() in ("quit", "exit", "q"):
                u = agent.get_token_usage()
                if u["call_count"] > 0:
                    print()
                    cost_str = f"，约 ¥{u['cost_yuan']:.4f}" if u.get("cost_yuan") is not None else ""
                    print(hint(f"本会话共调用 LLM {u['call_count']} 次，合计 token: {u['total_tokens']}（输入 {u['prompt_tokens']} + 输出 {u['completion_tokens']}）{cost_str}"))
                print()
                print(label("再见！祝你生活愉快！"))
                print()
                break

            if user_input.lower() == "clear":
                agent.clear_context()
                print(hint("  对话历史已清空。"))
                print(thin_separator())
                continue

            if user_input.lower() == "help":
                print_help()
                print(thin_separator())
                continue

            if user_input.lower() in ("usage", "stats", "tokens"):
                print_token_usage(agent)
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
                        # 有文本连续输出时临时隐藏 spinner，避免“逐帧换行感”
                        if spinner_paused or (time.monotonic() - last_text_output_ts < 0.35):
                            with io_lock:
                                if spinner_line_active:
                                    _erase_spinner_line()
                            await asyncio.sleep(0.03)
                            continue
                        with io_lock:
                            # 再次检查，避免 pause 与写入之间竞态导致“漏一帧”
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
                    """暂停 spinner → 擦除 → 打印 → 恢复"""
                    nonlocal last_text_output_ts
                    _pause_spinner()
                    with io_lock:
                        _erase_spinner_line()
                        print(text, end=end)
                        sys.stdout.flush()
                    last_text_output_ts = time.monotonic()
                    _resume_spinner()

                def _print_without_spinner(text: str = "", end: str = "\n") -> None:
                    """在 spinner 关闭状态下打印（不自动恢复）。"""
                    nonlocal last_text_output_ts
                    with io_lock:
                        _erase_spinner_line()
                        print(text, end=end)
                        sys.stdout.flush()
                    last_text_output_ts = time.monotonic()

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

                # ── Live 管理 ──

                def _teardown_live() -> None:
                    """拆除 Live 显示（transient=True 会自动清除内容）"""
                    nonlocal live, stream_started
                    if live is not None:
                        try:
                            live.__exit__(None, None, None)
                        except Exception:
                            pass
                        live = None
                    stream_started = False

                def _emit_draft_snapshot() -> None:
                    """将累积的流式内容以 dim 文本输出为草稿"""
                    nonlocal stream_buffer
                    draft_text = (stream_buffer or "").strip()
                    stream_buffer = ""
                    if show_draft == "off" or not draft_text:
                        return
                    if show_draft == "summary":
                        draft_text = " ".join(draft_text.split())
                        if len(draft_text) > draft_max_chars:
                            draft_text = draft_text[: max(0, draft_max_chars - 3)] + "..."
                    _print_without_spinner(t(f"  草稿：{draft_text}", dim=True))

                def _flush_reasoning_buffer() -> None:
                    nonlocal reasoning_buffer, reasoning_started
                    text = reasoning_buffer.strip()
                    reasoning_buffer = ""
                    if text:
                        _print_with_spinner(t(text, dim=True))
                        reasoning_started = True

                # ── 流式回调 ──

                def on_stream_delta(delta: str) -> None:
                    """正式回复：Rich Live Markdown 动态渲染"""
                    nonlocal stream_started, stream_buffer, live, last_render_ts, last_text_output_ts
                    if not delta:
                        return
                    stream_buffer += delta

                    if not stream_started:
                        _pause_spinner()
                        with io_lock:
                            _erase_spinner_line()
                        # 给 spinner 一点时间完全停下，避免首帧残留
                        try:
                            time.sleep(0.03)
                        except Exception:
                            pass
                        _flush_reasoning_buffer()
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
                            _pause_spinner()
                            with io_lock:
                                _erase_spinner_line()
                            _teardown_live()
                            _emit_draft_snapshot()
                        last_render_ts = 0.0
                        _resume_spinner()
                        iteration = event.get("iteration")
                        tool_count = event.get("tool_count")
                        _print_with_spinner()
                        _print_with_spinner(hint(f"  第 {iteration} 步: 调用模型（可用工具 {tool_count}）"))
                    elif event_type == "tool_call":
                        _flush_reasoning_buffer()
                        if stream_started:
                            _pause_spinner()
                            with io_lock:
                                _erase_spinner_line()
                            _teardown_live()
                            _emit_draft_snapshot()
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

                response = await agent.process_input(
                    user_input,
                    on_stream_delta=on_stream_delta,
                    on_reasoning_delta=on_reasoning_delta,
                    on_trace_event=on_trace_event,
                )
                _stop_spinner()
                _flush_reasoning_buffer()
                if spinner_task is not None:
                    await spinner_task

                # ── 最终回复渲染 ──
                if stream_started and live is not None:
                    # Live 正在显示最终回复 → 切为持久化，避免闪烁
                    live.update(Markdown(response), refresh=True)
                    live.transient = False
                    try:
                        live.__exit__(None, None, None)
                    except Exception:
                        pass
                    live = None
                    print()
                else:
                    # 无 Live（无 rich 或未进入流式）→ 静态渲染
                    print()
                    if _HAS_RICH and _RICH_CONSOLE is not None:
                        _RICH_CONSOLE.print(Markdown(response))
                    else:
                        print(response)
                    print()

                u = agent.get_token_usage()
                delta = u["total_tokens"] - prev_total_tokens
                prev_total_tokens = u["total_tokens"]
                cost = u.get("cost_yuan")
                print(status_bar(u["total_tokens"], u["call_count"], delta, cost))

            except Exception as e:
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

        except KeyboardInterrupt:
            print()
            print(hint("检测到中断信号，正在退出..."))
            print()
            break
        except EOFError:
            print()
            print(label("再见！"))
            print()
            break
