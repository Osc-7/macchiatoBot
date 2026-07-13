"""Web dashboard for config and kernel management."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

import yaml
import uuid

from fastapi import APIRouter, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.staticfiles import StaticFiles as StarletteStaticFiles
from starlette.responses import Response
from pydantic import BaseModel

from agent_core.config import find_config_file
from agent_core.interfaces import AgentHooks, AgentRunInput
from frontend.dashboard.auth import (
    DashboardAuth,
    DashboardAuthConfig,
    DashboardAuthMiddleware,
)
from frontend.dashboard.paths import CONSOLE_PREFIX, LOGIN_PATH, console_path
from system.automation.ipc import AutomationIPCClient

logger = logging.getLogger(__name__)


class NoCacheStaticFiles(StarletteStaticFiles):
    """StaticFiles that always disables browser caching for dashboard assets."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope,
        status_code: int = 200,
    ) -> Response:
        resp = super().file_response(full_path, stat_result, scope, status_code)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp


async def _maybe_handle_slash(client: AutomationIPCClient, text: str) -> str | None:
    """Run the shared slash-command handler. Returns reply if handled, else None."""
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    from frontend.feishu.slash_commands import try_handle_slash_command

    try:
        handled, reply = await try_handle_slash_command(client, stripped)
    except Exception as exc:  # noqa: BLE001
        return f"Slash command failed: {exc}"
    if not handled:
        return None
    return reply or "(no output)"


class ConfigUpdateRequest(BaseModel):
    """Config update payload."""

    yaml_text: str | None = None
    content: Dict[str, Any] | None = None


class SessionActionRequest(BaseModel):
    """Session action payload."""

    session_id: str


class ConfigBackupCreateRequest(BaseModel):
    reason: str | None = None


class ConfigRestoreRequest(BaseModel):
    backup_name: str


class ModelSwitchRequest(BaseModel):
    name: str


class ChatRequest(BaseModel):
    text: str
    session_id: str | None = None


class KernelExecRequest(BaseModel):
    """Kernel console command payload."""

    command: str
    session_id: str | None = None


class PermissionResolveRequest(BaseModel):
    permission_id: str
    allowed: bool = False
    persist_acl: bool = False
    clarify_requested: bool = False
    user_instruction: str | None = None
    path_prefix: str | None = None
    note: str | None = None


class AskUserResolveRequest(BaseModel):
    batch_id: str
    answers: list[Dict[str, Any]]


class LoginRequest(BaseModel):
    username: str | None = None
    password: str


KERNEL_CONSOLE_HELP = """Available commands:

Diagnostics
  help                       Show this help.
  ping                       Probe the daemon (pong/no response).
  ps                         List active cores (terminal_ps).
  top                        Snapshot of kernel top metrics.
  queue                      Inspect the run queue.
  jobs                       List automation jobs.
  cron                       Alias of jobs (scheduled automations).
  tasks [limit]              Recent agent tasks (default 25).
  inspect <session_id>       Detailed core inspection.

Sessions / Models
  sessions                   List session ids.
  models                     List configured models.
  usage                      Token usage for the active session.
  turns                      Turn count for the active session.

Mutating actions
  spawn  <session_id>        Spawn a core.
  cancel <session_id>        Cancel an in-flight core.
  kill   <session_id>        Forcefully kill a core.
  attach <session_id> <msg>  Send a system message and get a reply.

Users / memory
  user list [--frontend cli]           List memory users.
  user create <id> [--frontend cli] [--warm]

Pass-through
  /help, /clear, /usage, /session ..., /model ...,
  /compress, /dangerously on|off|status, /remote-status, …
  Anything starting with '/' is forwarded to the daemon slash handler.
  Bare commands are also retried as `/<command>` if the verb is unknown."""


def _format_console_ps(cores: Any) -> str:
    items = cores if isinstance(cores, list) else []
    if not items:
        return "(no active cores)"
    lines = [
        f"{'SESSION':<34} {'SOURCE':<10} {'USER':<12} {'MODE':<8} {'STATE':<10} {'IDLE':>5} {'TOKENS':>7} {'TURNS':>5}",
        "-" * 96,
    ]
    for raw in items:
        if not isinstance(raw, dict):
            continue
        lines.append(
            f"{str(raw.get('session_id', ''))[:34]:<34} "
            f"{str(raw.get('source', ''))[:10]:<10} "
            f"{str(raw.get('user_id', ''))[:12]:<12} "
            f"{str(raw.get('mode', ''))[:8]:<8} "
            f"{str(raw.get('lifecycle', ''))[:10]:<10} "
            f"{int(raw.get('idle_seconds', 0) or 0):>5} "
            f"{int(raw.get('total_tokens', 0) or 0):>7} "
            f"{int(raw.get('turn_count', 0) or 0):>5}"
        )
    lines.append(f"\n{len(items)} core(s)")
    return "\n".join(lines)


def _format_console_top(data: Dict[str, Any]) -> str:
    uptime = float(data.get("uptime_seconds", 0) or 0)
    return "\n".join(
        [
            f"  active cores     {data.get('active_cores', 0)} / {data.get('max_cores', 0)}",
            f"  queue depth      {data.get('queue_depth', 0)}",
            f"  inflight tasks   {data.get('inflight_tasks', 0)}",
            f"  zombie cores     {data.get('zombie_cores', 0)}",
            f"  uptime           {uptime:.0f}s ({uptime / 60:.1f}m)",
        ]
    )


def _format_console_queue(data: Dict[str, Any]) -> str:
    inflight = data.get("inflight_sessions") or {}
    cancelled = data.get("cancelled_sessions") or []
    lines = [
        f"  queue size           {data.get('queue_size', 0)}",
        f"  active task count    {data.get('active_task_count', 0)}",
        f"  inflight sessions    {len(inflight) if isinstance(inflight, dict) else inflight}",
    ]
    if isinstance(inflight, dict) and inflight:
        lines.append("  inflight detail:")
        for sid, count in inflight.items():
            lines.append(f"    · {sid}  ({count})")
    if cancelled:
        lines.append(f"  cancelled sessions   {len(cancelled)}")
        for sid in cancelled[:8]:
            lines.append(f"    · {sid}")
        if len(cancelled) > 8:
            lines.append(f"    … +{len(cancelled) - 8} more")
    return "\n".join(lines)


def _format_console_jobs(data: Dict[str, Any]) -> str:
    if not data.get("available"):
        return data.get("message") or "(automation scheduler unavailable)"
    lines = [
        f"  scheduler running    {data.get('scheduler_running')}",
        f"  reload interval      {data.get('reload_interval_seconds')}s",
        f"  tracked jobs         {data.get('tracked_job_count', 0)}",
        "",
    ]
    jobs = [j for j in (data.get("jobs") or []) if isinstance(j, dict)]
    if not jobs:
        lines.append("(no tracked jobs)")
        return "\n".join(lines)
    for idx, job in enumerate(jobs, start=1):
        jid = job.get("job_name", "?")
        dfn = job.get("definition") or {}
        name = dfn.get("name") or jid
        alive = not job.get("task_done") and not job.get("task_cancelled")
        state = "alive" if alive else ("cancelled" if job.get("task_cancelled") else "done")
        schedule = "unknown"
        if dfn.get("one_shot") and dfn.get("run_at"):
            schedule = f"once @ {dfn['run_at']}"
        elif dfn.get("times"):
            schedule = f"daily {','.join(dfn['times'])}"
        elif dfn.get("daily_time"):
            schedule = f"daily {dfn['daily_time']}"
        elif dfn.get("interval_seconds"):
            schedule = f"every {dfn['interval_seconds']}s"
        lines.append(f"[{idx}/{len(jobs)}] {name}  ·  {state}")
        lines.append(f"  schedule   {schedule}")
        if dfn.get("instruction_preview"):
            preview = str(dfn["instruction_preview"]).replace("\n", " ")
            if len(preview) > 80:
                preview = preview[:77] + "…"
            lines.append(f"  instruction {preview}")
        if job.get("task_error"):
            lines.append(f"  error      {job['task_error']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_console_tasks(data: Dict[str, Any]) -> str:
    if not data.get("available"):
        return data.get("message") or "(agent task queue unavailable)"
    lines = [
        f"  pending          {data.get('pending_count', 0)}",
        f"  running          {data.get('running_count', 0)}",
        "",
    ]
    items = [x for x in (data.get("recent_tasks") or []) if isinstance(x, dict)]
    if not items:
        lines.append("(no recent tasks)")
        return "\n".join(lines)
    for idx, item in enumerate(items, start=1):
        tid = item.get("task_id") or "?"
        status = item.get("status") or "?"
        lines.append(f"[{idx}/{len(items)}] {tid}  ·  {status}")
        for key in ("source", "session_id", "user_id", "created_at", "started_at", "finished_at"):
            if item.get(key):
                lines.append(f"  {key:<14} {item[key]}")
        instr = str(item.get("instruction") or "").replace("\n", " ")
        if instr:
            if len(instr) > 100:
                instr = instr[:97] + "…"
            lines.append(f"  instruction    {instr}")
        if item.get("error"):
            lines.append(f"  error          {item['error']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_console_inspect(data: Dict[str, Any]) -> str:
    if not data:
        return "(empty)"

    labels = {
        "session_id": "Session",
        "source": "Source",
        "user_id": "User",
        "mode": "Mode",
        "lifecycle": "Lifecycle",
        "uptime_seconds": "Uptime",
        "idle_seconds": "Idle",
        "ttl_remaining_seconds": "TTL remaining",
        "turn_count": "Turns",
        "context_message_count": "Messages in context",
        "memory_enabled": "Memory",
        "has_checkpoint": "Checkpoint",
        "log_file": "Log file",
        "parent_session_id": "Parent session",
        "task_description": "Task",
        "completed_at": "Completed at",
        "sub_error": "Sub-agent error",
        "sub_result_preview": "Sub-agent result",
        "in_zombie": "Zombie",
    }

    def _fmt_scalar(key: str, value: Any) -> str:
        if value is None:
            return "-"
        if key.endswith("_seconds") and isinstance(value, (int, float)):
            sec = float(value)
            return f"{sec:.0f}s ({sec / 60:.1f}m)" if sec >= 60 else f"{sec:.0f}s"
        if isinstance(value, bool):
            return "yes" if value else "no"
        return str(value)

    lines: list[str] = [f"Session: {data.get('session_id', '?')}", ""]
    for key, value in data.items():
        if key in ("session_id", "token_usage", "profile_summary"):
            continue
        label = labels.get(key, key.replace("_", " ").title())
        lines.append(f"  {label:<18} {_fmt_scalar(key, value)}")

    usage = data.get("token_usage")
    if isinstance(usage, dict) and usage:
        lines.append("")
        lines.append("  Token usage:")
        for uk, uv in usage.items():
            lines.append(f"    {uk:<16} {uv}")

    profile = data.get("profile_summary")
    if isinstance(profile, dict) and profile:
        lines.append("")
        lines.append("  Profile:")
        for pk, pv in profile.items():
            lines.append(f"    {pk:<16} {pv}")

    return "\n".join(lines)


def _format_console_users(data: Dict[str, Any]) -> str:
    users = data.get("users") or []
    frontend = data.get("frontend") or "cli"
    if not isinstance(users, list) or not users:
        return f"frontend={frontend}  (no users)"
    lines = [f"  frontend   {frontend}", f"  users      {len(users)}", ""]
    for idx, name in enumerate(users, start=1):
        lines.append(f"  {idx:>2}. {name}")
    return "\n".join(lines)


def _format_console_user_create(data: Dict[str, Any]) -> str:
    lines = [
        f"  memory owner        {data.get('memory_owner', '-')}",
        f"  default session     {data.get('default_session_id', '-')}",
        f"  owner dir           {data.get('owner_dir', '-')}",
    ]
    created = data.get("created_paths") or []
    if created:
        lines.append(f"  created paths       {', '.join(created)}")
    if data.get("warm_spawn"):
        lines.append(f"  warm spawn          {'spawned' if data.get('spawned') else 'skipped'}")
    users = data.get("all_users_on_frontend")
    if isinstance(users, list):
        lines.append(f"  users on frontend   {', '.join(users) if users else '(none)'}")
    return "\n".join(lines)


def _format_console_sessions(sessions: Any) -> str:
    items = sessions if isinstance(sessions, list) else []
    if not items:
        return "(no sessions)"
    return "\n".join(f"  {idx + 1:>2}. {sid}" for idx, sid in enumerate(items))


def _format_console_models(models: Any) -> str:
    items = models if isinstance(models, list) else []
    if not items:
        return "(no models configured)"
    lines: list[str] = []
    for m in items:
        if not isinstance(m, dict):
            continue
        name = m.get("name") or "?"
        model_id = m.get("model") or "-"
        marker = " *active*" if m.get("active") else ""
        lines.append(f"  {name}{marker}")
        lines.append(f"    model   {model_id}")
        if m.get("provider"):
            lines.append(f"    provider {m['provider']}")
    return "\n".join(lines)


def _format_console_usage(data: Dict[str, Any]) -> str:
    cost = data.get("cost_yuan")
    cost_text = f"{float(cost):.4f}" if cost is not None else "0"
    return "\n".join(
        [
            f"  prompt tokens        {data.get('prompt_tokens', 0)}",
            f"  completion tokens    {data.get('completion_tokens', 0)}",
            f"  total tokens         {data.get('total_tokens', 0)}",
            f"  call count           {data.get('call_count', 0)}",
            f"  cache hit tokens     {data.get('prompt_cache_hit_tokens', 0)}",
            f"  cost (¥)             {cost_text}",
        ]
    )


def _format_console_spawn(data: Dict[str, Any]) -> str:
    sid = data.get("session_id") or "?"
    return "\n".join(
        [
            f"  spawned   {sid}",
            f"  mode      {data.get('mode', '-')}",
            f"  user      {data.get('user_id', '-')}",
            f"  source    {data.get('source', '-')}",
            f"  lifecycle {data.get('lifecycle', 'running')}",
        ]
    )


class DashboardBackend:
    """Backend service for dashboard APIs."""

    def __init__(self, *, config_path: Path | None = None) -> None:
        self._config_path = config_path

    def resolve_config_path(self) -> Path:
        if self._config_path is not None:
            return self._config_path
        env_override = os.environ.get("MACCHIATO_DASHBOARD_CONFIG_PATH", "").strip()
        if env_override:
            return Path(env_override).expanduser()
        return find_config_file()

    def read_config(self) -> Dict[str, Any]:
        cfg_path = self.resolve_config_path()
        if not cfg_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {cfg_path}")
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("配置文件必须是对象结构")
        return raw

    def _backup_dir(self) -> Path:
        return self.resolve_config_path().parent / ".dashboard_backups"

    # ── Chat history: read directly from the daemon's canonical DB ──
    def _daemon_chat_db_path(self, session_id: str | None = None) -> Path | None:
        """Locate the daemon's chat_history.db for a given session.

        Session IDs follow the pattern ``{frontend}:{owner_id}`` (e.g.
        ``cli:root``, ``feishu:ou_xxx``, ``web:admin``).  The canonical DB
        lives under ``data/memory/{frontend}/{owner_id}/chat_history.db``.
        """
        project_root = Path(os.environ.get("MACCHIATO_PROJECT_ROOT", "/home/ubuntu/macchiatoBot"))

        if session_id:
            parts = session_id.split(":", 1)
            if len(parts) == 2:
                frontend, owner_id = parts
                daemon_db = project_root / "data" / "memory" / frontend / owner_id / "chat_history.db"
                if daemon_db.exists():
                    return daemon_db

        # Fallback to legacy cli/root for backward compatibility
        fallback = project_root / "data" / "memory" / "cli" / "root" / "chat_history.db"
        return fallback if fallback.exists() else None

    def get_chat_history(self, session_id: str, limit: int = 100) -> List[Dict[str, str]]:
        """Return user/assistant messages for a session from the daemon's DB."""
        db_path = self._daemon_chat_db_path(session_id)
        if not db_path:
            return []
        try:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT role, content, timestamp FROM messages"
                    " WHERE session_id = ? AND role IN ('user', 'assistant')"
                    " ORDER BY id DESC LIMIT ?",
                    (session_id, limit),
                )
                rows = [dict(row) for row in cur.fetchall()]
                rows.reverse()
                return rows
        except Exception:
            return []

    def _validate_backup_name(self, backup_name: str) -> str:
        name = backup_name.strip()
        if not name or "/" in name or "\\" in name:
            raise ValueError("非法备份名")
        return name

    def list_backups(self) -> list[Dict[str, Any]]:
        backup_dir = self._backup_dir()
        if not backup_dir.exists():
            return []
        items: list[Dict[str, Any]] = []
        for path in sorted(backup_dir.glob("*.yaml"), reverse=True):
            st = path.stat()
            items.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size": st.st_size,
                    "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
                }
            )
        return items

    def create_backup(self, *, reason: str | None = None) -> Dict[str, Any]:
        cfg_path = self.resolve_config_path()
        if not cfg_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {cfg_path}")
        backup_dir = self._backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = (reason or "manual").strip().replace(" ", "_")[:30]
        safe_suffix = "".join(ch for ch in suffix if ch.isalnum() or ch in ("_", "-"))
        name = f"{stamp}-{safe_suffix or 'manual'}.yaml"
        backup_path = backup_dir / name
        backup_path.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
        self.prune_backups(max_keep=30)
        return {"name": name, "path": str(backup_path)}

    def prune_backups(self, *, max_keep: int = 30) -> None:
        backups = sorted(self._backup_dir().glob("*.yaml"), reverse=True)
        for stale in backups[max_keep:]:
            stale.unlink(missing_ok=True)

    def restore_backup(self, backup_name: str) -> Path:
        name = self._validate_backup_name(backup_name)
        backup = self._backup_dir() / name
        if not backup.exists():
            raise FileNotFoundError(f"备份不存在: {name}")
        cfg_path = self.resolve_config_path()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        return cfg_path

    def write_config(self, content: Dict[str, Any]) -> Path:
        cfg_path = self.resolve_config_path()
        if cfg_path.exists():
            self.create_backup(reason="autosave")
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        dumped = yaml.safe_dump(
            content,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        cfg_path.write_text(dumped, encoding="utf-8")
        return cfg_path

    def write_config_text(self, yaml_text: str) -> Path:
        cfg_path = self.resolve_config_path()
        if cfg_path.exists():
            self.create_backup(reason="autosave")
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(yaml_text, encoding="utf-8")
        return cfg_path

    # ------------------------------------------------------------------
    # Session isolation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_admin(username: str) -> bool:
        return username.strip().lower() == "admin"

    @staticmethod
    def _web_session_prefix(username: str) -> str:
        return f"web:{username.strip()}" if username.strip() else "web:root"

    def _filter_sessions_for_user(
        self, sessions: list[str], username: str
    ) -> list[str]:
        if self._is_admin(username):
            return list(sessions)
        prefix = self._web_session_prefix(username)
        filtered = [s for s in sessions if s.startswith(prefix)]
        # Ensure the user's default session always appears in the dropdown
        # even if it hasn't been created yet on the gateway.
        if prefix not in filtered:
            filtered.append(prefix)
        return filtered

    def _filter_cores_for_user(
        self, cores: list[dict], username: str
    ) -> list[dict]:
        if self._is_admin(username):
            return list(cores)
        prefix = self._web_session_prefix(username)
        return [c for c in cores if str(c.get("session_id") or "").startswith(prefix)]

    def _assert_session_access(self, session_id: str | None, username: str) -> None:
        """Raise 403 if the user is not allowed to touch this session."""
        if not session_id:
            return
        if self._is_admin(username):
            return
        prefix = self._web_session_prefix(username)
        if not session_id.startswith(prefix):
            raise HTTPException(status_code=403, detail="Access denied to this session")

    async def _with_client(
        self, username: str = "", timeout_seconds: float | None = None
    ) -> AutomationIPCClient:
        owner_id = username.strip() or "root"
        source = "web" if username.strip() else "dashboard"
        kwargs: Dict[str, Any] = {
            "source": source,
            "owner_id": owner_id,
            "username": username,
            "client_id": f"dashboard-{owner_id}",
        }
        if timeout_seconds is not None:
            kwargs["timeout_seconds"] = timeout_seconds
        client = AutomationIPCClient(**kwargs)
        await client.connect()
        return client

    async def kernel_snapshot(self, username: str = "") -> Dict[str, Any]:
        client = await self._with_client(username=username)
        try:
            top = await client.terminal_top()
            queue = await client.terminal_queue()
            cores = await client.terminal_ps()
            users = await client.terminal_list_users(frontend="cli")
            sessions = await client.list_sessions()
            models = await client.list_models()
            token_usage = await client.get_token_usage()
            turn_count = await client.get_turn_count()
            dangerous_mode = await client.get_dangerous_mode()
            active_session_id = client.active_session_id
            # Filter visibility for non-admin users
            if not self._is_admin(username):
                sessions = self._filter_sessions_for_user(sessions, username)
                cores = self._filter_cores_for_user(cores, username)
                # If the active session is outside the user's scope, pin it to the
                # user's default web session so the frontend dropdown stays consistent.
                if not active_session_id.startswith(self._web_session_prefix(username)):
                    active_session_id = self._web_session_prefix(username)
            return {
                "connected": True,
                "top": top,
                "queue": queue,
                "cores": cores,
                "users": users,
                "sessions": sessions,
                "active_session_id": active_session_id,
                "models": models,
                "token_usage": token_usage,
                "turn_count": turn_count,
                "dangerous_mode": dangerous_mode,
            }
        finally:
            await client.close()

    async def kernel_kill(self, session_id: str) -> None:
        client = await self._with_client()
        try:
            await client.terminal_kill(session_id=session_id)
        finally:
            await client.close()

    async def kernel_cancel(self, session_id: str) -> bool:
        client = await self._with_client()
        try:
            return await client.terminal_cancel(session_id=session_id)
        finally:
            await client.close()

    async def kernel_spawn(self, session_id: str) -> Dict[str, Any]:
        client = await self._with_client()
        try:
            return await client.terminal_spawn(session_id=session_id, source="dashboard")
        finally:
            await client.close()

    async def switch_session(
        self, session_id: str, *, username: str = ""
    ) -> Dict[str, Any]:
        client = await self._with_client(username=username)
        try:
            created = await client.switch_session(session_id=session_id)
            return {"active_session_id": client.active_session_id, "created": created}
        finally:
            await client.close()

    async def clear_context(self, *, username: str = "") -> None:
        client = await self._with_client(username=username)
        try:
            await client.clear_context()
        finally:
            await client.close()

    async def switch_model(self, name: str, *, username: str = "") -> Dict[str, Any]:
        client = await self._with_client(username=username)
        try:
            info = await client.switch_model(name=name)
            return {"active_session_id": client.active_session_id, "info": info}
        finally:
            await client.close()

    async def chat(self, text: str, *, session_id: str | None = None, username: str = "") -> Dict[str, Any]:
        client = await self._with_client(username=username)
        try:
            if session_id:
                await client.switch_session(session_id=session_id)
            elif username:
                await client.switch_session(session_id=self._web_session_prefix(username))
            slash_reply = await _maybe_handle_slash(client, text)
            if slash_reply is not None:
                usage = await client.get_token_usage()
                turn_count = await client.get_turn_count()
                return {
                    "output_text": slash_reply,
                    "attachments": [],
                    "metadata": {"slash": True},
                    "session_id": client.active_session_id,
                    "token_usage": usage,
                    "turn_count": turn_count,
                }
            result = await client.run_turn(
                AgentRunInput(text=text, metadata={"source": "dashboard"})
            )
            usage = await client.get_token_usage()
            turn_count = await client.get_turn_count()
            return {
                "output_text": result.output_text,
                "attachments": list(getattr(result, "attachments", []) or []),
                "metadata": dict(result.metadata or {}),
                "session_id": client.active_session_id,
                "token_usage": usage,
                "turn_count": turn_count,
            }
        finally:
            await client.close()

    async def chat_stream(
        self, text: str, *, session_id: str | None = None, username: str = ""
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream chat events (assistant_delta / reasoning_delta / trace / final)."""
        # Use a long timeout so ask_user / permission waits do not kill the IPC stream.
        client = await self._with_client(username=username, timeout_seconds=7200.0)
        try:
            if session_id:
                await client.switch_session(session_id=session_id)
            elif username:
                await client.switch_session(session_id=self._web_session_prefix(username))

            slash_reply = await _maybe_handle_slash(client, text)
            if slash_reply is not None:
                yield {"type": "system", "message": slash_reply}
                try:
                    usage = await client.get_token_usage()
                except Exception:  # noqa: BLE001
                    usage = {}
                try:
                    turn_count = await client.get_turn_count()
                except Exception:  # noqa: BLE001
                    turn_count = 0
                yield {
                    "type": "final",
                    "ok": True,
                    "output_text": slash_reply,
                    "metadata": {"slash": True},
                    "attachments": [],
                    "session_id": client.active_session_id,
                    "token_usage": usage,
                    "turn_count": turn_count,
                }
                return

            queue: asyncio.Queue[Any] = asyncio.Queue()
            SENTINEL = object()

            async def _on_assistant_delta(delta: str) -> None:
                if delta:
                    await queue.put({"type": "assistant_delta", "delta": delta})

            async def _on_reasoning_delta(delta: str) -> None:
                if delta:
                    await queue.put({"type": "reasoning_delta", "delta": delta})

            async def _on_trace_event(evt: Dict[str, Any]) -> None:
                await queue.put({"type": "trace", "data": evt})

            async def _on_permission_notify(
                permission_id: str, payload: Dict[str, Any]
            ) -> None:
                await queue.put(
                    {
                        "type": "permission_request",
                        "permission_id": permission_id,
                        "payload": payload,
                    }
                )

            async def _on_ask_user_notify(
                batch_id: str, payload: Dict[str, Any]
            ) -> None:
                logger.info(
                    "chat_stream ask_user notify: batch_id=%s questions=%d",
                    batch_id,
                    len(payload.get("questions") or []),
                )
                await queue.put(
                    {
                        "type": "ask_user",
                        "batch_id": batch_id,
                        "payload": payload,
                    }
                )

            hooks = AgentHooks(
                on_assistant_delta=_on_assistant_delta,
                on_reasoning_delta=_on_reasoning_delta,
                on_trace_event=_on_trace_event,
                on_feishu_permission_notify=_on_permission_notify,
                on_feishu_ask_user_notify=_on_ask_user_notify,
            )

            holder: Dict[str, Any] = {"result": None, "error": None}

            async def _runner() -> None:
                try:
                    holder["result"] = await client.run_turn(
                        AgentRunInput(text=text, metadata={"source": "dashboard"}),
                        hooks=hooks,
                    )
                except Exception as exc:  # noqa: BLE001
                    holder["error"] = exc
                finally:
                    await queue.put(SENTINEL)

            task = asyncio.create_task(_runner())
            _http_disconnect = False
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # SSE keepalive for mobile proxies / Safari that drop
                        # idle connections after ~30-60 s.
                        yield {"type": "_keepalive"}
                        continue
                    if event is SENTINEL:
                        break
                    yield event
                await task
                if holder["error"]:
                    yield {"type": "error", "message": str(holder["error"])}
                    return
                result = holder["result"]
                try:
                    usage = await client.get_token_usage()
                except Exception:  # noqa: BLE001
                    usage = {}
                try:
                    turn_count = await client.get_turn_count()
                except Exception:  # noqa: BLE001
                    turn_count = 0
                yield {
                    "type": "final",
                    "ok": True,
                    "output_text": result.output_text if result else "",
                    "metadata": dict(result.metadata or {}) if result else {},
                    "attachments": (
                        list(getattr(result, "attachments", []) or [])
                        if result
                        else []
                    ),
                    "session_id": client.active_session_id,
                    "token_usage": usage,
                    "turn_count": turn_count,
                }
            except GeneratorExit:
                # HTTP connection dropped (e.g. Safari mobile timeout).
                # Do NOT cancel the IPC task – that would close the IPC writer
                # while the server may still be streaming, causing BrokenPipe.
                # Let the turn finish naturally; the server buffers the result.
                _http_disconnect = True
                raise
            except asyncio.CancelledError:
                _http_disconnect = True
                raise
            except Exception:
                # Genuine error in this coroutine – cancel the IPC task.
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except Exception:  # noqa: BLE001
                        pass
                raise
            finally:
                if not _http_disconnect and not task.done():
                    task.cancel()
                    try:
                        await task
                    except Exception:  # noqa: BLE001
                        pass
                # NOTE: 不再在 HTTP 被动断开时取消 turn。
                # Safari 切后台/网络抖动属于“被动离开”，应让 turn 跑完并将结果
                # 存入对话历史；用户重新进页面时通过 /chat/history 即可看到完整
                # 回复。若此处强制 cancel，会导致回复丢失（用户看到 Load failed）。
        finally:
            await client.close()

    async def get_stream_recoveries(
        self, *, session_id: str | None = None, username: str = ""
    ) -> list[Dict[str, Any]]:
        """Poll recoveries from IPC server and filter to sessions this user may access."""
        client = await self._with_client(username=username)
        try:
            recoveries = await client.poll_stream_recoveries()
            if not isinstance(recoveries, list):
                return []
            if username and not self._is_admin(username):
                prefix = self._web_session_prefix(username)
                recoveries = [
                    r
                    for r in recoveries
                    if str(r.get("session_id") or "").startswith(prefix)
                ]
            if session_id:
                recoveries = [
                    r for r in recoveries if r.get("session_id") == session_id
                ]
            return recoveries
        finally:
            await client.close()

    async def resolve_permission(self, payload: PermissionResolveRequest) -> Dict[str, Any]:
        client = await self._with_client()
        try:
            ok = await client.resolve_permission(
                permission_id=payload.permission_id.strip(),
                allowed=payload.allowed,
                persist_acl=payload.persist_acl,
                clarify_requested=payload.clarify_requested,
                user_instruction=(payload.user_instruction or "").strip() or None,
                path_prefix=(payload.path_prefix or "").strip() or None,
                note=(payload.note or "").strip() or None,
            )
            return {"ok": bool(ok)}
        finally:
            await client.close()

    async def resolve_ask_user(self, payload: AskUserResolveRequest) -> Dict[str, Any]:
        client = await self._with_client()
        try:
            ok = await client.resolve_ask_user(
                batch_id=payload.batch_id.strip(),
                answers=list(payload.answers or []),
            )
            return {"ok": bool(ok)}
        finally:
            await client.close()

    async def kernel_exec(
        self, command: str, *, session_id: str | None = None, username: str = ""
    ) -> Dict[str, Any]:
        """Run a command through the kernel console.

        Supports a small set of native diagnostics plus slash-command
        pass-through. Returns ``{ok, kind, output, data?}``.
        """

        raw = (command or "").strip()
        if not raw:
            return {"ok": False, "kind": "error", "output": "(empty command)"}

        self._assert_session_access(session_id, username)

        client = await self._with_client(username=username)
        try:
            if session_id:
                try:
                    await client.switch_session(session_id=session_id)
                except Exception:  # noqa: BLE001
                    pass

            if raw.startswith("/"):
                reply = await _maybe_handle_slash(client, raw)
                if reply is None:
                    return {
                        "ok": False,
                        "kind": "error",
                        "output": f"unknown slash command: {raw}",
                    }
                return {"ok": True, "kind": "slash", "output": reply}

            try:
                parts = shlex.split(raw)
            except ValueError as exc:
                return {"ok": False, "kind": "error", "output": f"parse error: {exc}"}
            if not parts:
                return {"ok": False, "kind": "error", "output": "(empty command)"}
            verb = parts[0].lower()
            args = parts[1:]

            try:
                if verb in ("help", "?"):
                    return {"ok": True, "kind": "text", "output": KERNEL_CONSOLE_HELP}
                if verb in ("ps", "cores"):
                    data = await client.terminal_ps()
                    if username and not self._is_admin(username):
                        data = self._filter_cores_for_user(data, username)
                    return {
                        "ok": True,
                        "kind": "text",
                        "output": _format_console_ps(data),
                        "data": data,
                    }
                if verb == "top":
                    data = await client.terminal_top()
                    return {
                        "ok": True,
                        "kind": "text",
                        "output": _format_console_top(data),
                        "data": data,
                    }
                if verb == "queue":
                    data = await client.terminal_queue()
                    return {
                        "ok": True,
                        "kind": "text",
                        "output": _format_console_queue(data),
                        "data": data,
                    }
                if verb in ("jobs", "cron", "automation"):
                    data = await client.terminal_automation_jobs()
                    return {
                        "ok": True,
                        "kind": "text",
                        "output": _format_console_jobs(data),
                        "data": data,
                    }
                if verb == "tasks":
                    limit = 25
                    if args and args[0].isdigit():
                        limit = max(1, min(200, int(args[0])))
                    data = await client.terminal_agent_tasks(limit=limit)
                    return {
                        "ok": True,
                        "kind": "text",
                        "output": _format_console_tasks(data),
                        "data": data,
                    }
                if verb == "inspect":
                    if not args:
                        return {
                            "ok": False,
                            "kind": "error",
                            "output": "usage: inspect <session_id>",
                        }
                    try:
                        self._assert_session_access(args[0], username)
                    except HTTPException as exc:
                        return {"ok": False, "kind": "error", "output": exc.detail}
                    data = await client.terminal_inspect(args[0])
                    return {
                        "ok": True,
                        "kind": "text",
                        "output": _format_console_inspect(data),
                        "data": data,
                    }
                if verb == "ping":
                    ok = await client.ping()
                    return {
                        "ok": bool(ok),
                        "kind": "text",
                        "output": "pong" if ok else "no response",
                    }
                if verb == "kill":
                    if not args:
                        return {
                            "ok": False,
                            "kind": "error",
                            "output": "usage: kill <session_id>",
                        }
                    try:
                        self._assert_session_access(args[0], username)
                    except HTTPException as exc:
                        return {"ok": False, "kind": "error", "output": exc.detail}
                    await client.terminal_kill(args[0])
                    return {"ok": True, "kind": "text", "output": f"killed: {args[0]}"}
                if verb == "cancel":
                    if not args:
                        return {
                            "ok": False,
                            "kind": "error",
                            "output": "usage: cancel <session_id>",
                        }
                    try:
                        self._assert_session_access(args[0], username)
                    except HTTPException as exc:
                        return {"ok": False, "kind": "error", "output": exc.detail}
                    cancelled = await client.terminal_cancel(args[0])
                    return {
                        "ok": True,
                        "kind": "text",
                        "output": f"cancel({args[0]}) → {cancelled}",
                    }
                if verb == "spawn":
                    if not args:
                        return {
                            "ok": False,
                            "kind": "error",
                            "output": "usage: spawn <session_id>",
                        }
                    try:
                        self._assert_session_access(args[0], username)
                    except HTTPException as exc:
                        return {"ok": False, "kind": "error", "output": exc.detail}
                    data = await client.terminal_spawn(
                        session_id=args[0], source="dashboard"
                    )
                    return {
                        "ok": True,
                        "kind": "text",
                        "output": _format_console_spawn(data if isinstance(data, dict) else {}),
                        "data": data,
                    }
                if verb in ("sessions", "session-list"):
                    data = await client.list_sessions()
                    if username and not self._is_admin(username):
                        data = self._filter_sessions_for_user(data, username)
                    return {
                        "ok": True,
                        "kind": "text",
                        "output": _format_console_sessions(data),
                        "data": data,
                    }
                if verb == "models":
                    data = await client.list_models()
                    return {
                        "ok": True,
                        "kind": "text",
                        "output": _format_console_models(data),
                        "data": data,
                    }
                if verb == "usage":
                    data = await client.get_token_usage()
                    return {
                        "ok": True,
                        "kind": "text",
                        "output": _format_console_usage(data if isinstance(data, dict) else {}),
                        "data": data,
                    }
                if verb == "turns":
                    n = await client.get_turn_count()
                    return {"ok": True, "kind": "text", "output": str(n)}
                if verb == "attach":
                    if len(args) < 2:
                        return {
                            "ok": False,
                            "kind": "error",
                            "output": "usage: attach <session_id> <message...>",
                        }
                    sid = args[0]
                    try:
                        self._assert_session_access(sid, username)
                    except HTTPException as exc:
                        return {"ok": False, "kind": "error", "output": exc.detail}
                    text = " ".join(args[1:])
                    result = await client.terminal_attach(sid, text)
                    output = getattr(result, "output_text", None) or "(no text reply)"
                    return {"ok": True, "kind": "text", "output": output}
                if verb == "user":
                    if not args:
                        return {
                            "ok": False,
                            "kind": "error",
                            "output": "usage: user list [--frontend cli] | user create <id> [--frontend cli] [--warm]",
                        }
                    sub = args[0].lower()
                    rest = args[1:]
                    if sub == "list":
                        frontend = "cli"
                        i = 0
                        while i < len(rest):
                            if rest[i] == "--frontend" and i + 1 < len(rest):
                                frontend = rest[i + 1]
                                i += 2
                            else:
                                i += 1
                        data = await client.terminal_list_users(frontend=frontend)
                        return {
                            "ok": True,
                            "kind": "text",
                            "output": _format_console_users(data if isinstance(data, dict) else {}),
                            "data": data,
                        }
                    if sub == "create":
                        if not rest:
                            return {
                                "ok": False,
                                "kind": "error",
                                "output": "usage: user create <user_id> [--frontend cli] [--warm]",
                            }
                        uid = rest[0]
                        frontend = "cli"
                        warm = False
                        i = 1
                        while i < len(rest):
                            if rest[i] == "--frontend" and i + 1 < len(rest):
                                frontend = rest[i + 1]
                                i += 2
                            elif rest[i] == "--warm":
                                warm = True
                                i += 1
                            else:
                                i += 1
                        data = await client.terminal_create_user(
                            uid, frontend=frontend, warm_spawn=warm
                        )
                        return {
                            "ok": True,
                            "kind": "text",
                            "output": _format_console_user_create(
                                data if isinstance(data, dict) else {}
                            ),
                            "data": data,
                        }
                    return {
                        "ok": False,
                        "kind": "error",
                        "output": f"unknown user subcommand: {sub}",
                    }
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "kind": "error",
                    "output": f"{verb} failed: {exc}",
                }

            reply = await _maybe_handle_slash(client, "/" + raw)
            if reply is not None:
                return {"ok": True, "kind": "slash", "output": reply}

            return {
                "ok": False,
                "kind": "error",
                "output": (
                    f"unknown command: {verb}\n"
                    "Type `help` to list available commands."
                ),
            }
        finally:
            await client.close()

    async def ping_daemon(self) -> Dict[str, Any]:
        try:
            client = AutomationIPCClient(source="dashboard", owner_id="root")
            ok = await client.ping()
            return {"connected": bool(ok)}
        except Exception as exc:  # noqa: BLE001
            return {"connected": False, "error": str(exc)}

def create_dashboard_app(
    *,
    backend: DashboardBackend | None = None,
    auth: DashboardAuth | None = None,
) -> FastAPI:
    """Create dashboard FastAPI app."""
    app = FastAPI(title="macchiato dashboard")
    service = backend or DashboardBackend()
    config_dir: Path | None = None
    if hasattr(service, "resolve_config_path"):
        try:
            config_dir = service.resolve_config_path().parent
        except Exception:  # noqa: BLE001
            config_dir = None
    dashboard_auth = auth or DashboardAuth(DashboardAuthConfig.load(config_dir=config_dir))
    app.state.dashboard_auth = dashboard_auth
    app.add_middleware(DashboardAuthMiddleware, auth=dashboard_auth)

    static_dir = Path(__file__).with_name("static")
    app.mount(
        console_path("/assets"),
        NoCacheStaticFiles(directory=static_dir),
        name="dashboard-assets",
    )

    console = APIRouter(prefix=CONSOLE_PREFIX)

    @app.get(LOGIN_PATH)
    async def login_page() -> FileResponse:
        return FileResponse(static_dir / "login.html")

    @app.get("/")
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(console_path("/"), status_code=302)

    @app.get(CONSOLE_PREFIX, include_in_schema=False)
    async def console_redirect() -> RedirectResponse:
        return RedirectResponse(console_path("/"), status_code=302)

    @console.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            static_dir / "index.html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @console.get("/api/auth/status")
    async def auth_status_api(request: Request) -> Dict[str, Any]:
        subject = dashboard_auth.authenticate_request(request)
        return dashboard_auth.status(authenticated=bool(subject), subject=subject or "")

    @console.post("/api/auth/login")
    async def auth_login_api(payload: LoginRequest) -> JSONResponse:
        subject = dashboard_auth.verify_credentials(
            (payload.username or "").strip(),
            payload.password or "",
        )
        if subject is None:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        resp = JSONResponse(
            {"ok": True, **dashboard_auth.status(authenticated=True, subject=subject)}
        )
        dashboard_auth.set_session_cookie(resp, subject)
        return resp

    @console.post("/api/auth/logout")
    async def auth_logout_api() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        dashboard_auth.clear_session_cookie(resp)
        return resp

    @console.get("/api/config")
    async def get_config_api() -> Dict[str, Any]:
        try:
            content = service.read_config()
            cfg_path = service.resolve_config_path()
            return {
                "path": str(cfg_path),
                "content": content,
                "yaml_text": yaml.safe_dump(
                    content,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                ),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("dashboard read config failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.put("/api/config")
    async def put_config_api(payload: ConfigUpdateRequest) -> Dict[str, Any]:
        try:
            if payload.yaml_text is not None:
                parsed = yaml.safe_load(payload.yaml_text) or {}
                if not isinstance(parsed, dict):
                    raise ValueError("配置必须是 YAML 对象")
                path = service.write_config_text(payload.yaml_text)
            elif payload.content is not None:
                path = service.write_config(payload.content)
            else:
                raise ValueError("payload 不能为空")
            return {"ok": True, "path": str(path)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("dashboard write config failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.get("/api/config/backups")
    async def get_config_backups_api() -> Dict[str, Any]:
        try:
            return {"items": service.list_backups()}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.post("/api/config/backups")
    async def post_config_backup_api(
        payload: ConfigBackupCreateRequest,
    ) -> Dict[str, Any]:
        try:
            backup = service.create_backup(reason=payload.reason)
            return {"ok": True, "backup": backup}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.post("/api/config/restore")
    async def post_config_restore_api(payload: ConfigRestoreRequest) -> Dict[str, Any]:
        try:
            path = service.restore_backup(payload.backup_name)
            return {"ok": True, "path": str(path)}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.get("/api/kernel")
    async def get_kernel_api(request: Request) -> JSONResponse:
        try:
            username = getattr(request.state, "dashboard_user", "")
            data = await service.kernel_snapshot(username=username)
            return JSONResponse(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dashboard kernel snapshot failed: %s", exc)
            return JSONResponse(
                {
                    "connected": False,
                    "error": str(exc),
                    "top": {},
                    "queue": {},
                    "cores": [],
                    "users": {},
                    "sessions": [],
                    "active_session_id": "",
                    "models": [],
                    "token_usage": {},
                    "turn_count": 0,
                    "dangerous_mode": {},
                },
                status_code=200,
            )

    @console.post("/api/kernel/kill")
    async def post_kernel_kill(
        payload: SessionActionRequest, request: Request
    ) -> Dict[str, Any]:
        try:
            username = getattr(request.state, "dashboard_user", "")
            service._assert_session_access(payload.session_id.strip(), username)
            await service.kernel_kill(payload.session_id.strip())
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.post("/api/kernel/cancel")
    async def post_kernel_cancel(
        payload: SessionActionRequest, request: Request
    ) -> Dict[str, Any]:
        try:
            username = getattr(request.state, "dashboard_user", "")
            service._assert_session_access(payload.session_id.strip(), username)
            cancelled = await service.kernel_cancel(payload.session_id.strip())
            return {"ok": True, "cancelled": cancelled}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.post("/api/kernel/spawn")
    async def post_kernel_spawn(
        payload: SessionActionRequest, request: Request
    ) -> Dict[str, Any]:
        try:
            username = getattr(request.state, "dashboard_user", "")
            service._assert_session_access(payload.session_id.strip(), username)
            core = await service.kernel_spawn(payload.session_id.strip())
            return {"ok": True, "core": core}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.post("/api/kernel/session/switch")
    async def post_kernel_session_switch(
        payload: SessionActionRequest, request: Request
    ) -> Dict[str, Any]:
        try:
            username = getattr(request.state, "dashboard_user", "")
            service._assert_session_access(payload.session_id.strip(), username)
            return await service.switch_session(
                payload.session_id.strip(), username=username
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.post("/api/kernel/context/clear")
    async def post_kernel_context_clear(request: Request) -> Dict[str, Any]:
        try:
            username = getattr(request.state, "dashboard_user", "")
            await service.clear_context(username=username)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.post("/api/kernel/model/switch")
    async def post_kernel_model_switch(
        payload: ModelSwitchRequest, request: Request
    ) -> Dict[str, Any]:
        try:
            username = getattr(request.state, "dashboard_user", "")
            return await service.switch_model(payload.name.strip(), username=username)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.post("/api/kernel/exec")
    async def post_kernel_exec(payload: KernelExecRequest, request: Request) -> Dict[str, Any]:
        username = getattr(request.state, "dashboard_user", "")
        session_id = (payload.session_id or "").strip() or None
        service._assert_session_access(session_id, username)
        try:
            return await service.kernel_exec(
                payload.command,
                session_id=session_id,
                username=username,
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.get("/api/health")
    async def get_health_api() -> Dict[str, Any]:
        return await service.ping_daemon()

    @console.post("/api/chat")
    async def post_chat_api(payload: ChatRequest, request: Request) -> Dict[str, Any]:
        text = (payload.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        username = getattr(request.state, "dashboard_user", "")
        service._assert_session_access(
            (payload.session_id or "").strip() or None, username
        )
        try:
            return await service.chat(
                text,
                session_id=(payload.session_id or "").strip() or None,
                username=username,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.post("/api/permission/resolve")
    async def post_permission_resolve(payload: PermissionResolveRequest) -> Dict[str, Any]:
        pid = (payload.permission_id or "").strip()
        if not pid:
            raise HTTPException(status_code=400, detail="permission_id is required")
        try:
            return await service.resolve_permission(payload)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.post("/api/ask-user/resolve")
    async def post_ask_user_resolve(payload: AskUserResolveRequest) -> Dict[str, Any]:
        bid = (payload.batch_id or "").strip()
        if not bid:
            raise HTTPException(status_code=400, detail="batch_id is required")
        try:
            return await service.resolve_ask_user(payload)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.post("/api/chat/stream")
    async def post_chat_stream_api(payload: ChatRequest, request: Request) -> StreamingResponse:
        text = (payload.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        session_id = (payload.session_id or "").strip() or None
        username = getattr(request.state, "dashboard_user", "")
        service._assert_session_access(session_id, username)

        async def gen() -> AsyncIterator[bytes]:
            try:
                async for event in service.chat_stream(
                    text, session_id=session_id, username=username
                ):
                    if event.get("type") == "_keepalive":
                        yield b": \n"
                        continue
                    yield (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            except Exception as exc:  # noqa: BLE001
                yield (
                    json.dumps(
                        {"type": "error", "message": str(exc)}, ensure_ascii=False
                    )
                    + "\n"
                ).encode("utf-8")

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    @console.post("/api/chat/recoveries")
    async def post_chat_recoveries(request: Request) -> JSONResponse:
        username = getattr(request.state, "dashboard_user", "")
        try:
            recoveries = await service.get_stream_recoveries(username=username)
            return JSONResponse({"recoveries": recoveries})
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.post("/api/chat/upload")
    async def post_chat_upload(file: UploadFile) -> Dict[str, Any]:
        if not file.filename:
            raise HTTPException(status_code=400, detail="no file selected")
        # Sanitise filename and ensure uniqueness
        safe_name = Path(file.filename or "upload").name
        stem, suffix = os.path.splitext(safe_name)
        unique_name = f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"
        upload_dir = Path(os.environ.get("MACCHIATO_DASHBOARD_UPLOAD_DIR", "/tmp/macchiato_uploads"))
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / unique_name
        content = await file.read()
        dest.write_bytes(content)
        logger.info("Uploaded %s → %s (%d bytes)", file.filename, dest, len(content))
        return {
            "ok": True,
            "filename": file.filename,
            "path": str(dest),
            "size": len(content),
        }

    @console.get("/api/chat/history")
    async def get_chat_history_api(request: Request) -> JSONResponse:
        username = getattr(request.state, "dashboard_user", "")
        session_id = (request.query_params.get("session_id") or "").strip()
        limit = request.query_params.get("limit", "100")
        try:
            limit_num = int(limit)
        except ValueError:
            limit_num = 100
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        service._assert_session_access(session_id, username)
        try:
            history = service.get_chat_history(session_id, limit=limit_num)
            return JSONResponse({"history": history})
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @console.post("/api/chat/history/clear")
    async def post_chat_history_clear_api(request: Request) -> JSONResponse:
        username = getattr(request.state, "dashboard_user", "")
        session_id = (request.query_params.get("session_id") or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        service._assert_session_access(session_id, username)
        # Daemon owns the canonical DB; dashboard only clears its own view.
        return JSONResponse({"ok": True})

    app.include_router(console)

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        create_dashboard_app(),
        host=os.environ.get("MACCHIATO_DASHBOARD_HOST", "127.0.0.1"),
        port=int(os.environ.get("MACCHIATO_DASHBOARD_PORT", "8765")),
    )


if __name__ == "__main__":
    main()
