"""
Kernel shell — interactive control plane for the automation daemon (KernelTerminal).

Run: ``python main.py shell`` or ``python -m system.kernel.shell``
Requires: ``automation_daemon.py`` running (with PYTHONPATH if needed).
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from system.automation import AutomationIPCClient

_SECTION_W = 62
# `  ╭` (3 cols) + inner = 62 → inner = 59
_BOX_INNER = _SECTION_W - 3
# Text between `│ ` and `│` on a panel row (62 cols total).
_PANEL_TEXT_W = _SECTION_W - 5

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


@dataclass
class _Term:
    """ANSI styling when stdout is a TTY and NO_COLOR is unset (POSIX/Linux/macOS 惯例)."""

    use_color: bool

    @property
    def reset(self) -> str:
        return "\033[0m" if self.use_color else ""

    @property
    def bold(self) -> str:
        return "\033[1m" if self.use_color else ""

    @property
    def dim(self) -> str:
        return "\033[2m" if self.use_color else ""

    @property
    def cyan(self) -> str:
        return "\033[36m" if self.use_color else ""

    @property
    def blue(self) -> str:
        return "\033[34m" if self.use_color else ""

    @property
    def green(self) -> str:
        return "\033[32m" if self.use_color else ""

    @property
    def yellow(self) -> str:
        return "\033[33m" if self.use_color else ""

    @property
    def magenta(self) -> str:
        return "\033[35m" if self.use_color else ""


def _use_color() -> bool:
    if os.environ.get("NO_COLOR", "").strip():
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _hr(term: _Term, ch: str = "─", width: int = _SECTION_W) -> None:
    print(f"{term.dim}{ch * width}{term.reset}")


def _header(term: _Term, title: str) -> None:
    """一级区块：顶栏标题（类似 systemctl status / htop 分区）。"""
    left = "  ╭─ "
    mid = _SECTION_W - len(left) - len(title) - 3  # 末尾 ` ─` + `╮`
    if mid < 0:
        mid = 0
    print(
        f"{term.dim}{left}{term.reset}{term.bold}{term.cyan}{title}{term.reset}"
        f"{term.dim} ─{'─' * mid}╮{term.reset}"
    )
    _hr(term, "─", _SECTION_W)


def _panel_top(term: _Term, subtitle: str = "") -> None:
    if subtitle:
        start = f"─ {subtitle} "
        if len(start) > _BOX_INNER:
            start = start[: _BOX_INNER]
        mid = start + "─" * max(0, _BOX_INNER - len(start))
    else:
        mid = "─" * _BOX_INNER
    print(f"{term.dim}  ╭{mid}{term.reset}")


def _panel_bottom(term: _Term) -> None:
    print(f"{term.dim}  ╰{'─' * _BOX_INNER}{term.reset}")


def _panel_line(term: _Term, text: str, *, pad_to: int = _PANEL_TEXT_W) -> None:
    """Box 内一行：`│` + 文本（截断到宽度；支持行内 ANSI，按可见宽度对齐）。"""
    t = text.replace("\n", " ")
    if _visible_len(t) > pad_to:
        # 仅对无 ANSI 的截断做简化处理；过长时截断可见字符
        plain = _ANSI_RE.sub("", t)
        if len(plain) > pad_to:
            t = plain[: pad_to - 1] + "…"
    pad = max(0, pad_to - _visible_len(t))
    print(f"{term.dim}  │{term.reset} {t}{' ' * pad}{term.dim}│{term.reset}")


def _kv(
    term: _Term,
    key: str,
    value: Any,
    *,
    key_width: int = 16,
    indent: int = 4,
    key_style: Optional[str] = None,
) -> None:
    v = value if value is not None and value != "" else "n/a"
    ks = key_style or term.dim
    pad = " " * indent
    print(f"{pad}{ks}{key:<{key_width}}{term.reset} {v}")


def _kv_boxed(
    term: _Term,
    key: str,
    value: Any,
    *,
    key_width: int = 14,
) -> None:
    v = value if value is not None and value != "" else "n/a"
    line = f" {key:<{key_width}} {v}"
    _panel_line(term, line, pad_to=_PANEL_TEXT_W)


def _welcome_banner(term: _Term) -> None:
    _hr(term, "═", _SECTION_W)
    print(
        f"{term.bold}{term.cyan}  Kernel control shell{term.reset}"
        f" {term.dim}(KernelTerminal){term.reset}"
    )
    print(f"{term.dim}  Commands:{term.reset} ps | top | queue | cron | tasks | user … | inspect | kill | …")
    print(f"{term.dim}  Type{term.reset} help {term.dim}for usage ·{term.reset} quit {term.dim}/{term.reset} exit {term.dim}/{term.reset} q {term.dim}to leave.{term.reset}")
    _hr(term, "─", _SECTION_W)
    print()


def _parse_dt(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_dt(raw: Any) -> str:
    dt = _parse_dt(raw)
    if dt is None:
        return "n/a"
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + " UTC"


def _elapsed_seconds(start: Any, end: Any) -> Optional[float]:
    a, b = _parse_dt(start), _parse_dt(end)
    if a is None or b is None:
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return (b - a).total_seconds()


def _elapsed_since_start(raw_start: Any) -> Optional[float]:
    a = _parse_dt(raw_start)
    if a is None:
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - a).total_seconds()


def _job_state_badge(term: _Term, stat: str) -> str:
    s = stat.lower()
    if s == "alive":
        return f"{term.green}{stat}{term.reset}"
    if s == "cancelled":
        return f"{term.yellow}{stat}{term.reset}"
    return f"{term.dim}{stat}{term.reset}"


def _task_status_badge(term: _Term, st: str) -> str:
    u = st.upper()
    if u in ("SUCCESS", "OK", "DONE"):
        return f"{term.green}{u}{term.reset}"
    if u in ("RUNNING", "PENDING"):
        return f"{term.yellow}{u}{term.reset}"
    if u in ("FAILED", "ERROR", "CANCELLED"):
        return f"{term.magenta}{u}{term.reset}"
    return f"{term.bold}{u}{term.reset}"


def _print_instruction_block(
    term: _Term,
    text: str,
    *,
    max_lines: int = 4,
    line_max: int = 52,
) -> None:
    t = (text or "").strip()
    if not t:
        return
    lines = t.replace("\r\n", "\n").split("\n")
    shown = 0
    for i, line in enumerate(lines):
        if shown >= max_lines:
            _panel_line(term, f"... ({len(lines)} lines total, truncated)")
            break
        chunk = line[:line_max] + ("..." if len(line) > line_max else "")
        prefix = "instruction " if i == 0 else "            "
        _panel_line(term, f"{prefix}{chunk}")
        shown += 1


def _print_table(
    term: _Term,
    rows: List[Dict[str, Any]],
    columns: List[str],
    widths: Optional[Dict[str, int]] = None,
) -> None:
    widths = widths or {}
    for col in columns:
        if col not in widths:
            widths[col] = max(len(str(col)), max((len(str(r.get(col, ""))) for r in rows), default=0))
    fmt = "  ".join(f"{{:{widths.get(c, 12)}}}" for c in columns)
    head = fmt.format(*columns)
    print(f"{term.dim}  {head}{term.reset}")
    rule_len = sum(widths.get(c, 12) for c in columns) + 2 * (len(columns) - 1)
    print(f"{term.dim}  {'─' * rule_len}{term.reset}")
    for r in rows:
        print(f"  {fmt.format(*(str(r.get(c, '')) for c in columns))}")


async def run_kernel_shell(client: AutomationIPCClient) -> None:
    term = _Term(_use_color())
    _welcome_banner(term)

    while True:
        try:
            try:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("kernel> ").strip()
                )
            except EOFError:
                print()
                break
            if not line:
                continue

            parts = line.split()
            cmd = parts[0].lower()
            args = parts[1:]

            if cmd in ("quit", "exit", "q"):
                print(f"{term.dim}Bye.{term.reset}")
                break

            if cmd == "help":
                print()
                _header(term, "Help")
                print(f"{term.bold}{term.blue}  Command reference{term.reset}")
                _hr(term, "·", 40)
                print(f"{term.dim}  ps{term.reset}")
                print("      List loaded Agent cores (live sessions in this daemon).")
                print(f"{term.dim}  top{term.reset}")
                print("      Summary: core count, scheduler queue depth, inflight tasks, uptime.")
                print(f"{term.dim}  queue{term.reset}")
                print("      KernelScheduler request queue and inflight session IDs.")
                print(f"{term.dim}  cron{term.reset}")
                print("      AutomationScheduler: job definitions and when each job fires.")
                print("      This is the schedule, not execution history.")
                print(f"{term.dim}  tasks [N]{term.reset}")
                print("      Persistent AgentTask queue (SQLite): pending, running, and last N")
                print("      finished tasks with timestamps and durations.")
                print("      Different from cron: tasks are actual enqueue/run records.")
                print(f"{term.dim}  inspect <session_id>{term.reset}")
                print("      Dump one core's detail dict.")
                print(f"{term.dim}  kill <session_id>{term.reset}     Tear down a core.")
                print(f"{term.dim}  cancel <session_id>{term.reset}   Cancel the in-flight turn for that session.")
                print(f"{term.dim}  spawn <session_id> [--source X] [--user Y]{term.reset}")
                print("      Create a core.")
                print(f"{term.dim}  attach <session_id> <text>{term.reset}")
                print("      Send a system message into a session.")
                print(f"{term.dim}  user create <user_id> [--frontend cli] [--warm]{term.reset}")
                print(f"{term.dim}  user list [--frontend cli]{term.reset}")
                print("      Memory owner dirs for a frontend.")
                print()
                print(f"{term.dim}  help | quit | exit | q{term.reset}")
                print()
                continue

            if cmd == "ps":
                cores = await client.terminal_ps()
                _header(term, "PS")
                if not cores:
                    print(f"{term.dim}  (no active cores){term.reset}")
                else:
                    rows = []
                    for c in cores:
                        r = dict(c)
                        r["idle_seconds"] = int(r.get("idle_seconds", 0))
                        rows.append(r)
                    _print_table(
                        term,
                        rows,
                        ["session_id", "source", "user_id", "mode", "idle_seconds", "total_tokens", "turn_count"],
                        {"session_id": 32, "source": 10, "user_id": 12, "mode": 10},
                    )
                print()
                continue

            if cmd == "top":
                status = await client.terminal_top()
                _header(term, "TOP")
                _panel_top(term)
                _kv(term, "active_cores", status.get("active_cores", 0))
                _kv(term, "max_cores", status.get("max_cores", 0))
                _kv(term, "queue_depth", status.get("queue_depth", 0))
                _kv(term, "inflight_tasks", status.get("inflight_tasks", 0))
                _kv(term, "uptime_seconds", f"{status.get('uptime_seconds', 0):.0f}")
                _panel_bottom(term)
                print()
                continue

            if cmd == "queue":
                q = await client.terminal_queue()
                _header(term, "QUEUE")
                _panel_top(term)
                _kv(term, "queue_size", q.get("queue_size", 0))
                _kv(term, "active_task_count", q.get("active_task_count", 0))
                _kv(term, "inflight_sessions", q.get("inflight_sessions", {}))
                _kv(term, "cancelled_sessions", q.get("cancelled_sessions", []))
                _panel_bottom(term)
                print()
                continue

            if cmd in ("cron", "automation"):
                snap = await client.terminal_automation_jobs()
                if not snap.get("available"):
                    print(f"{term.dim}  ({snap.get('message', 'unavailable')}){term.reset}")
                    continue
                _header(term, "CRON")
                _panel_top(term, "scheduler")
                _kv(term, "scheduler_running", snap.get("scheduler_running"))
                _kv(term, "reload_interval_s", snap.get("reload_interval_seconds"))
                w = snap.get("watcher")
                if isinstance(w, dict):
                    _kv(term, "watcher", f"name={w.get('name')} done={w.get('done')} cancelled={w.get('cancelled')}")
                _kv(term, "tracked_job_count", snap.get("tracked_job_count", 0))
                _panel_bottom(term)
                print()
                jobs = [j for j in (snap.get("jobs") or []) if isinstance(j, dict)]
                nj = len(jobs)
                for idx, j in enumerate(jobs, start=1):
                    jid = j.get("job_name", "")
                    dfn = j.get("definition") or {}
                    display_name = dfn.get("name") or jid
                    alive = not j.get("task_done") and not j.get("task_cancelled")
                    stat = "alive" if alive else ("cancelled" if j.get("task_cancelled") else "done")
                    schedule_parts = []
                    if dfn.get("one_shot") and dfn.get("run_at"):
                        schedule_parts.append(f"once @ {dfn['run_at']}")
                    elif dfn.get("times"):
                        schedule_parts.append(f"daily {','.join(dfn['times'])}")
                    elif dfn.get("daily_time"):
                        schedule_parts.append(f"daily {dfn['daily_time']}")
                    elif dfn.get("start_time"):
                        schedule_parts.append(f"from {dfn['start_time']} every {dfn.get('interval_seconds')}s")
                    else:
                        schedule_parts.append(f"every {dfn.get('interval_seconds')}s")
                    schedule = "  ".join(schedule_parts)
                    _panel_top(term, f"[{idx}/{nj}] {display_name}")
                    badge = _job_state_badge(term, stat)
                    _panel_line(term, f" track_state      {badge}")
                    _kv_boxed(term, "schedule", schedule)
                    if jid != display_name:
                        _kv_boxed(term, "job_name", jid)
                    if dfn.get("job_type"):
                        _kv_boxed(term, "job_type", dfn["job_type"])
                    if dfn.get("memory_owner"):
                        _kv_boxed(term, "memory_owner", dfn["memory_owner"])
                    if dfn.get("core_mode"):
                        _kv_boxed(term, "core_mode", dfn["core_mode"])
                    if dfn.get("timezone") and dfn["timezone"] != "Asia/Shanghai":
                        _kv_boxed(term, "timezone", dfn["timezone"])
                    if dfn.get("instruction_preview"):
                        _kv_boxed(term, "instruction", dfn["instruction_preview"])
                    if j.get("task_error"):
                        _kv_boxed(term, "scheduler_error", j.get("task_error"))
                    _panel_bottom(term)
                    print()
                if not jobs:
                    print(f"{term.dim}  (no tracked jobs){term.reset}")
                    print()
                continue

            if cmd in ("tasks", "agent_tasks"):
                lim = 25
                if args:
                    try:
                        lim = max(1, min(100, int(args[0])))
                    except ValueError:
                        print("  tasks: invalid limit. Usage: tasks [N]   (see help)")
                        continue
                tq = await client.terminal_agent_tasks(limit=lim)
                if not tq.get("available"):
                    print(f"{term.dim}  ({tq.get('message', 'unavailable')}){term.reset}")
                    continue
                _header(term, "TASKS")
                _panel_top(term, "queue summary")
                _kv(term, "pending", tq.get("pending_count", 0))
                _kv(term, "running", tq.get("running_count", 0))
                _kv(term, "showing_last", lim)
                _panel_bottom(term)
                print()
                items = [x for x in (tq.get("recent_tasks") or []) if isinstance(x, dict)]
                ni = len(items)
                for idx, item in enumerate(items, start=1):
                    st = str(item.get("status") or "?")
                    tid = item.get("task_id") or "?"
                    st_badge = _task_status_badge(term, st)
                    _panel_top(term, f"[{idx}/{ni}]  {tid}")
                    _panel_line(term, f" status           {st_badge}")
                    _kv_boxed(term, "source", item.get("source"))
                    _kv_boxed(term, "session_id", item.get("session_id"))
                    _kv_boxed(term, "user_id", item.get("user_id"))
                    _kv_boxed(term, "created_at", _fmt_dt(item.get("created_at")))
                    _kv_boxed(term, "started_at", _fmt_dt(item.get("started_at")))
                    fin = _fmt_dt(item.get("finished_at"))
                    _kv_boxed(term, "finished_at", fin)
                    el = _elapsed_seconds(item.get("started_at"), item.get("finished_at"))
                    if el is not None:
                        _kv_boxed(term, "run_duration", f"{el:.1f}s")
                    elif st in ("pending", "running") and item.get("started_at"):
                        el2 = _elapsed_since_start(item.get("started_at"))
                        if el2 is not None and el2 >= 0:
                            _kv_boxed(term, "running_for", f"{el2:.1f}s (since started_at)")
                    err = item.get("error")
                    if err:
                        _kv_boxed(term, "error", err)
                    md = item.get("metadata")
                    if isinstance(md, dict) and md:
                        parts = [f"{k}={v}" for k, v in sorted(md.items()) if v not in (None, "")]
                        if parts:
                            _kv_boxed(term, "metadata", " | ".join(parts))
                    _print_instruction_block(term, str(item.get("instruction") or ""))
                    _panel_bottom(term)
                    print()
                if not items:
                    print(f"{term.dim}  (no recent tasks){term.reset}")
                    print()
                continue

            if cmd == "inspect":
                if not args:
                    print("  Usage: inspect <session_id>   (see help)")
                    continue
                sid = args[0]
                try:
                    detail = await client.terminal_inspect(sid)
                    for k, v in detail.items():
                        print(f"  {k}: {v}")
                except Exception as e:
                    print(f"  error: {e}")
                continue

            if cmd == "kill":
                if not args:
                    print("  Usage: kill <session_id>   (see help)")
                    continue
                try:
                    await client.terminal_kill(args[0])
                    print(f"  killed: {args[0]}")
                except Exception as e:
                    print(f"  error: {e}")
                continue

            if cmd == "cancel":
                if not args:
                    print("  Usage: cancel <session_id>   (see help)")
                    continue
                try:
                    cancelled = await client.terminal_cancel(args[0])
                    print(f"  cancelled: {cancelled}")
                except Exception as e:
                    print(f"  error: {e}")
                continue

            if cmd == "spawn":
                if not args:
                    print("  Usage: spawn <session_id> [--source X] [--user Y]   (see help)")
                    continue
                sid = args[0]
                source, user = "system", "root"
                i = 1
                while i < len(args):
                    if args[i] == "--source" and i + 1 < len(args):
                        source = args[i + 1]
                        i += 2
                    elif args[i] == "--user" and i + 1 < len(args):
                        user = args[i + 1]
                        i += 2
                    else:
                        i += 1
                try:
                    info = await client.terminal_spawn(sid, source=source, user_id=user)
                    print(f"  spawned: {info.get('session_id', sid)}")
                except Exception as e:
                    print(f"  error: {e}")
                continue

            if cmd == "attach":
                if len(args) < 2:
                    print("  Usage: attach <session_id> <message...>   (see help)")
                    continue
                sid = args[0]
                text = " ".join(args[1:])
                try:
                    result = await client.terminal_attach(sid, text)
                    print()
                    print(result.output_text or "(no text reply)")
                    print()
                except Exception as e:
                    print(f"  error: {e}")
                continue

            if cmd == "user":
                if not args:
                    print("  Usage: user create <user_id> [--frontend cli] [--warm]")
                    print("          user list [--frontend cli]   (see help)")
                    continue
                sub = args[0].lower()
                rest = args[1:]
                if sub == "create":
                    if not rest:
                        print("  Usage: user create <user_id> [--frontend cli] [--warm]   (see help)")
                        continue
                    uid = rest[0]
                    fe, warm = "cli", False
                    i = 1
                    while i < len(rest):
                        if rest[i] == "--frontend" and i + 1 < len(rest):
                            fe = rest[i + 1]
                            i += 2
                        elif rest[i] == "--warm":
                            warm = True
                            i += 1
                        else:
                            i += 1
                    try:
                        out = await client.terminal_create_user(
                            uid, frontend=fe, warm_spawn=warm
                        )
                        print(f"  memory_owner: {out.get('memory_owner')}")
                        print(f"  default_session_id: {out.get('default_session_id')}")
                        print(f"  owner_dir: {out.get('owner_dir')}")
                        cp = out.get("created_paths") or []
                        if cp:
                            print(f"  created_paths: {cp}")
                        else:
                            print("  created_paths: (none; directories already existed)")
                        if out.get("warm_spawn"):
                            print(f"  warm_spawn: {'spawned' if out.get('spawned') else 'skipped'}")
                        users = out.get("all_users_on_frontend")
                        if isinstance(users, list):
                            print(f"  users_on_frontend: {', '.join(users) if users else '(none)'}")
                    except Exception as e:
                        print(f"  error: {e}")
                    continue
                if sub == "list":
                    fe = "cli"
                    i = 0
                    while i < len(rest):
                        if rest[i] == "--frontend" and i + 1 < len(rest):
                            fe = rest[i + 1]
                            i += 2
                        else:
                            i += 1
                    try:
                        snap = await client.terminal_list_users(frontend=fe)
                        u = snap.get("users") or []
                        print(f"  frontend={snap.get('frontend', fe)}  users={len(u)}")
                        for name in u:
                            print(f"    {name}")
                    except Exception as e:
                        print(f"  error: {e}")
                    continue
                print(f"  user: unknown subcommand {sub!r}. Try: user create | user list   (see help)")
                continue

            print(f"  kernel: command not found: {cmd!r}. Type 'help' for usage.")

        except KeyboardInterrupt:
            print()
            continue


def main() -> None:
    """Entry: python -m system.kernel.shell"""
    from system.automation import default_socket_path

    async def _main() -> None:
        socket_path = os.environ.get("SCHEDULE_AUTOMATION_SOCKET", "").strip() or default_socket_path()
        client = AutomationIPCClient(socket_path=socket_path)
        if not await client.ping():
            print(f"error: cannot reach automation daemon at {socket_path}", file=sys.stderr)
            print("hint: start it with PYTHONPATH=src python automation_daemon.py", file=sys.stderr)
            sys.exit(1)
        await client.connect()
        try:
            await run_kernel_shell(client)
        finally:
            await client.close()

    asyncio.run(_main())


if __name__ == "__main__":
    main()
