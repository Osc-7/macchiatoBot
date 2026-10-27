"""Conversation notices for remote workspace activate / release / re-inject."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Optional

from macchiato_remote.protocol import RemoteWorkspaceState

logger = logging.getLogger(__name__)

_workspace_switch_frontend_notify: ContextVar[
    Optional[Callable[[str], Awaitable[None]]]
] = ContextVar("workspace_switch_frontend_notify", default=None)

WORKSPACE_SWITCH_PREFIX = "[工作区切换]"
WORKSPACE_STATUS_PREFIX = "[工作区]"


def _remote_workspace_body(
    state: RemoteWorkspaceState,
    *,
    skill_count: Optional[int] = None,
    mcp_line: Optional[str] = None,
) -> str:
    """Shared remote workspace detail block (login / path / notes)."""
    device = state.device_label or state.login
    skill_line = ""
    if skill_count is not None:
        skill_line = f"\n已扫描技能数: {int(skill_count)}"

    mcp_extra = ""
    if mcp_line:
        mcp_extra = f"\n{mcp_line.strip()}"

    ttl_line = ""
    if state.expires_at is not None:
        import time

        remaining = max(0, int(state.expires_at - time.time()))
        ttl_line = f"\n租约剩余: 约 {remaining // 60} 分钟"

    return f"""后端: remote
远程登录: {state.login}
远程机器: {device}
权限档位: {state.profile}
逻辑挂载: {state.workspace_mount}
授权目录: {state.display_remote_path}{ttl_line}{skill_line}{mcp_extra}

说明:
- bash / 文件工具 / load_skill 现在作用在上述远程工作区，不是云服务器本机目录。
- 相对路径、~、{state.workspace_mount} 都指向该远程授权目录。
- 工作文件写 `.macchiato/`（journal / rules / skills / scratch / jobs / mcp.yaml）。
- 技能查找: `.macchiato/skills` → `.agents/skills`（同名前者优先）。
- 长期记忆 MEMORY.md 仍在 daemon 侧；不要把设备路径写成跨设备全局偏好。
- 访问工作区外路径需用户授权（/remote-grant、/remote-elevate）。""".strip()


def format_remote_workspace_switch_notice(
    state: RemoteWorkspaceState,
    *,
    reason: str = "activated",
    skill_count: Optional[int] = None,
    mcp_line: Optional[str] = None,
) -> str:
    """Human/agent-facing notice written into conversation history (not system).

    - activate / bound / inherited → ``[工作区切换]``
    - restored / restored_offline（daemon 重启后从 checkpoint 恢复绑定）→ ``[工作区]``
    - reinjected (e.g. after compress) → ``[工作区]``，只陈述当前位置，不提压缩
    """
    reason_s = (reason or "activated").strip() or "activated"
    body = _remote_workspace_body(
        state, skill_count=skill_count, mcp_line=mcp_line
    )

    if reason_s == "reinjected":
        return f"""{WORKSPACE_STATUS_PREFIX}
{body}
""".strip()

    if reason_s == "restored":
        return f"""{WORKSPACE_STATUS_PREFIX}
daemon 重启后已恢复远程工作区绑定
{body}
""".strip()

    if reason_s == "restored_offline":
        return f"""{WORKSPACE_STATUS_PREFIX}
daemon 重启后已恢复远程工作区绑定，但远程 worker 当前离线
{body}

- worker 尚未重连：bash / 文件工具会报「远程 worker 未连接」，请在远端机器启动 macchiato-remote，或用 /remote-release 回到云端工作区。
""".strip()

    if reason_s == "inherited":
        headline = "子会话继承远程工作区"
    elif reason_s == "bound":
        headline = "任务已绑定远程工作区"
    else:
        headline = "已切换到远程工作区"

    return f"""{WORKSPACE_SWITCH_PREFIX}
{headline}
{body}
""".strip()


def format_daemon_host_lines() -> str:
    """本进程所在机器的运行时描述。每次调用现算，不写死主机名。"""
    import platform

    hostname = (platform.node() or "").strip() or "unknown"
    system = (platform.system() or "").strip() or "unknown"
    release = (platform.release() or "").strip()
    machine = (platform.machine() or "").strip()
    system_line = f"{system} {release}".strip()
    if machine:
        system_line = f"{system_line} ({machine})"
    return f"daemon 主机: {hostname}\ndaemon 系统: {system_line}"


def format_local_workspace_switch_notice(
    *,
    previous: Optional[RemoteWorkspaceState] = None,
) -> str:
    """Notice when releasing remote mode and returning to the daemon machine."""
    prev_line = ""
    if previous is not None:
        prev_line = (
            f"\n已释放远程: login={previous.login}, "
            f"path={previous.display_remote_path}"
        )
    host = format_daemon_host_lines()
    return f"""{WORKSPACE_SWITCH_PREFIX}
已回到 daemon 所在机器
后端: daemon
{host}{prev_line}

说明:
- bash / 文件工具 / load_skill 现在作用在上面这台 daemon 上，不是远程 worker。
- 技能查找: `.macchiato/skills` → `.agents/skills`（daemon 本机）。
- 长期记忆仍按 memory_owner 映射到 daemon 侧 MEMORY.md。
""".strip()


def format_frontend_remote_switch_message(state: RemoteWorkspaceState) -> str:
    """给用户看的一行切换说明，不含写进仓库的机器名。"""
    device = (state.device_label or state.login or "").strip() or state.login
    path = state.display_remote_path
    return (
        f"macchiatoBot 已将工作区切换至 {device}（{state.login}），"
        f"工作目录 {path}，权限档位 {state.profile}。"
    )


def format_frontend_daemon_switch_message(
    *,
    previous: Optional[RemoteWorkspaceState] = None,
    workspace_dir: str = "",
) -> str:
    """切回 daemon 时推给前端的一行说明。"""
    host_line = format_daemon_host_lines().splitlines()[0]
    hostname = host_line.split(":", 1)[-1].strip() if ":" in host_line else host_line
    directory = f"，工作目录 {workspace_dir}" if (workspace_dir or "").strip() else ""
    left = ""
    if previous is not None:
        device = (previous.device_label or previous.login or "").strip()
        left = f" 已离开 {device or previous.login}。"
    return f"macchiatoBot 已将工作区切换回 daemon（{hostname}）{directory}。{left}".strip()


@asynccontextmanager
async def workspace_switch_notify_scope(
    forward: Callable[[str], Awaitable[None]],
):
    """run_turn_stream 期间挂上，把切换说明写回当前前端。"""
    token = _workspace_switch_frontend_notify.set(forward)
    try:
        yield
    finally:
        _workspace_switch_frontend_notify.reset(token)


async def notify_workspace_switch_frontend(message: str) -> None:
    """若当前轮有前端流，推送一条用户可见的工作区切换说明。没有流则跳过。"""
    text = (message or "").strip()
    if not text:
        return
    forward = _workspace_switch_frontend_notify.get()
    if forward is None:
        return
    try:
        await forward(text)
    except Exception:  # noqa: BLE001
        logger.warning("workspace switch frontend notify failed", exc_info=True)


def append_workspace_switch_notice(
    agent: Any,
    text: str,
    *,
    persist: bool = True,
) -> bool:
    """
    Append a workspace-switch user message to the live conversation context.

    Returns True if the message was appended. Persistence to ChatHistoryDB is
    best-effort and only attempted when memory is enabled.
    """
    notice = (text or "").strip()
    if not notice:
        return False
    ctx = getattr(agent, "_context", None)
    if ctx is None or not hasattr(ctx, "add_user_message"):
        return False
    ctx.add_user_message(notice)
    # Context changed; invalidate cached prompt token estimate if present.
    if hasattr(agent, "_last_prompt_tokens"):
        try:
            agent._last_prompt_tokens = None
        except Exception:
            pass
    if persist and getattr(agent, "_memory_enabled", False):
        _note_history_id(agent, _persist_workspace_notice(agent, notice))
    return True


def _note_history_id(agent: Any, msg_id: Optional[int]) -> None:
    if msg_id is None or not hasattr(agent, "_last_history_id"):
        return
    agent._last_history_id = max(
        int(getattr(agent, "_last_history_id", 0) or 0),
        int(msg_id),
    )


def _persist_workspace_notice(agent: Any, notice: str) -> Optional[int]:
    """Best-effort ChatHistoryDB write. Safe to run on a worker thread."""
    try:
        db = agent._require_chat_history_db()
        sid = getattr(agent, "_session_id", "") or ""
        source = getattr(agent, "_source", "cli") or "cli"
        if not sid:
            return None
        return int(
            db.write_message(
                session_id=sid,
                role="user",
                content=notice,
                source=source,
            )
        )
    except Exception:
        return None


async def append_workspace_switch_notice_async(
    agent: Any,
    text: str,
    *,
    persist: bool = True,
) -> bool:
    """Same as append_workspace_switch_notice, but the sqlite write leaves the event loop."""
    notice = (text or "").strip()
    if not notice:
        return False
    appended = append_workspace_switch_notice(agent, notice, persist=False)
    if not appended:
        return False
    if persist and getattr(agent, "_memory_enabled", False):
        msg_id = await asyncio.to_thread(_persist_workspace_notice, agent, notice)
        _note_history_id(agent, msg_id)
    return True


def reinject_remote_workspace_notice_if_active(agent: Any) -> bool:
    """After context compression, re-append remote state if still active."""
    from agent_core.remote.workspace_state import (
        get_remote_workspace_skills_index,
        get_remote_workspace_state,
    )

    sid = getattr(agent, "_session_id", "") or ""
    state = get_remote_workspace_state(sid)
    if state is None:
        return False
    skill_count = None
    idx = get_remote_workspace_skills_index(sid)
    if idx:
        # Rough count from bullet lines in the cached index.
        skill_count = sum(
            1 for line in idx.splitlines() if line.strip().startswith("- **")
        )
    notice = format_remote_workspace_switch_notice(
        state,
        reason="reinjected",
        skill_count=skill_count,
    )
    return append_workspace_switch_notice(agent, notice, persist=True)


async def reinject_remote_workspace_notice_if_active_async(agent: Any) -> bool:
    """Event-loop variant: ChatHistoryDB persist runs on a worker thread."""
    from agent_core.remote.workspace_state import (
        get_remote_workspace_skills_index,
        get_remote_workspace_state,
    )

    sid = getattr(agent, "_session_id", "") or ""
    state = get_remote_workspace_state(sid)
    if state is None:
        return False
    skill_count = None
    idx = get_remote_workspace_skills_index(sid)
    if idx:
        skill_count = sum(
            1 for line in idx.splitlines() if line.strip().startswith("- **")
        )
    notice = format_remote_workspace_switch_notice(
        state,
        reason="reinjected",
        skill_count=skill_count,
    )
    return await append_workspace_switch_notice_async(agent, notice, persist=True)
