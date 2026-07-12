"""Conversation notices for remote workspace activate / release / re-inject."""

from __future__ import annotations

from typing import Any, Optional

from macchiato_remote.protocol import RemoteWorkspaceState

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


def format_local_workspace_switch_notice(
    *,
    previous: Optional[RemoteWorkspaceState] = None,
) -> str:
    """Notice when releasing remote mode and returning to the local/cloud workspace."""
    prev_line = ""
    if previous is not None:
        prev_line = (
            f"\n已释放远程: login={previous.login}, "
            f"path={previous.display_remote_path}"
        )
    return f"""{WORKSPACE_SWITCH_PREFIX}
已回到本地 / 云端工作区
后端: local{prev_line}

说明:
- bash / 文件工具 / load_skill 恢复为当前会话的本地工作区。
- 技能查找: `.macchiato/skills` → `.agents/skills`。
- 长期记忆仍按 memory_owner 映射到 daemon 侧 MEMORY.md。
""".strip()


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
    if not persist:
        return True
    if not getattr(agent, "_memory_enabled", False):
        return True
    try:
        db = agent._require_chat_history_db()
        sid = getattr(agent, "_session_id", "") or ""
        source = getattr(agent, "_source", "cli") or "cli"
        if sid:
            msg_id = db.write_message(
                session_id=sid,
                role="user",
                content=notice,
                source=source,
            )
            if hasattr(agent, "_last_history_id"):
                agent._last_history_id = max(
                    int(getattr(agent, "_last_history_id", 0) or 0),
                    int(msg_id),
                )
    except Exception:
        pass
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
