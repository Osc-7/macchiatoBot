from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_core.context.conversation import ConversationContext
from agent_core.remote.workspace_notice import (
    WORKSPACE_STATUS_PREFIX,
    WORKSPACE_SWITCH_PREFIX,
    append_workspace_switch_notice,
    format_local_workspace_switch_notice,
    format_remote_workspace_switch_notice,
    reinject_remote_workspace_notice_if_active,
)
from agent_core.remote.workspace_state import (
    activate_remote_workspace,
    clear_remote_workspace_state,
    format_remote_workspace_prompt_suffix,
    get_remote_workspace_state,
    release_remote_workspace,
    remote_ttl_lapsed,
)
from macchiato_remote.protocol import REMOTE_WORKSPACE_MOUNT, RemoteWorkspaceState
from system.kernel.kernel import AgentKernel


@pytest.fixture(autouse=True)
def _clear_remote_state():
    clear_remote_workspace_state()
    yield
    clear_remote_workspace_state()


def test_remote_workspace_state_validates_required_fields():
    with pytest.raises(ValueError):
        RemoteWorkspaceState(
            session_id="",
            login="personal",
            requested_path="~/Project",
        )


def test_activate_and_release_remote_workspace_state():
    state = activate_remote_workspace(
        session_id="feishu:abc",
        login="personal",
        requested_path="~/Project",
        profile="dev",
        ttl_seconds=60,
    )

    assert state.workspace_mount == REMOTE_WORKSPACE_MOUNT
    assert state.login == "personal"
    assert get_remote_workspace_state("feishu:abc") == state

    released = release_remote_workspace("feishu:abc")
    assert released == state
    assert get_remote_workspace_state("feishu:abc") is None


@pytest.mark.asyncio
async def test_remote_ttl_lapsed_blocks_silent_local_fallback():
    """Expired remote lease must not silently fall back to local tool execution."""
    import time

    from agent_core.config import get_config
    from system.tools.load_skill_tool import LoadSkillTool

    activate_remote_workspace(
        session_id="cli:root",
        login="personal",
        requested_path="~/Project",
        ttl_seconds=1,
    )
    time.sleep(1.1)
    assert get_remote_workspace_state("cli:root") is None
    assert remote_ttl_lapsed("cli:root") is True

    tool = LoadSkillTool(get_config())
    result = await tool.execute(
        skill_name="any-skill",
        __execution_context__={
            "session_id": "cli:root",
            "source": "cli",
            "user_id": "root",
        },
    )
    assert result.success is False
    assert result.error == "REMOTE_TTL_EXPIRED"


def test_remote_prompt_suffix_is_empty_notice_moved_to_history():
    state = activate_remote_workspace(
        session_id="feishu:abc",
        login="personal",
        requested_path="~/Project",
        profile="dev",
        ttl_seconds=None,
    )

    assert format_remote_workspace_prompt_suffix(state) == ""


def test_format_remote_workspace_switch_notice():
    state = activate_remote_workspace(
        session_id="feishu:abc",
        login="personal",
        requested_path="~/Project",
        resolved_path="/Users/me/Project",
        device_label="mbp",
        ttl_seconds=None,
    )
    notice = format_remote_workspace_switch_notice(
        state, reason="activated", skill_count=2
    )
    assert notice.startswith(WORKSPACE_SWITCH_PREFIX)
    assert "远程登录: personal" in notice
    assert "mbp" in notice
    assert "/workspace" in notice
    assert "已扫描技能数: 2" in notice
    assert ".macchiato/skills" in notice


def test_format_local_workspace_switch_notice():
    prev = activate_remote_workspace(
        session_id="feishu:abc",
        login="personal",
        requested_path="~/Project",
    )
    notice = format_local_workspace_switch_notice(previous=prev)
    assert notice.startswith(WORKSPACE_SWITCH_PREFIX)
    assert "后端: local" in notice
    assert "personal" in notice


def test_append_and_reinject_workspace_notice():
    ctx = ConversationContext()
    agent = SimpleNamespace(
        _context=ctx,
        _session_id="feishu:abc",
        _memory_enabled=False,
        _last_prompt_tokens=123,
        _source="feishu",
    )
    activate_remote_workspace(
        session_id="feishu:abc",
        login="personal",
        requested_path="~/Project",
        device_label="mbp",
        ttl_seconds=None,
    )
    notice = format_remote_workspace_switch_notice(
        get_remote_workspace_state("feishu:abc"),
        reason="activated",
    )
    assert append_workspace_switch_notice(agent, notice) is True
    assert agent._last_prompt_tokens is None
    assert any(WORKSPACE_SWITCH_PREFIX in str(m.get("content")) for m in ctx.messages)

    # Simulate compression wiping history, then reinject.
    ctx.messages = [{"role": "user", "content": "kept turn"}]
    assert reinject_remote_workspace_notice_if_active(agent) is True
    reinjected = next(
        m for m in ctx.messages if str(m.get("content", "")).startswith(WORKSPACE_STATUS_PREFIX)
    )
    body = str(reinjected.get("content"))
    assert WORKSPACE_SWITCH_PREFIX not in body
    assert "压缩" not in body
    assert "远程登录: personal" in body
    assert "授权目录:" in body
    assert "/workspace" in body or "逻辑挂载:" in body


@pytest.mark.asyncio
async def test_kernel_compress_reinjects_remote_notice(monkeypatch):
    ctx = ConversationContext()
    # Enough turns to trigger compression with keep_recent_turns=1
    for i in range(4):
        ctx.add_user_message(f"user {i}")
        ctx.add_assistant_message(content=f"assistant {i}")

    agent = SimpleNamespace(
        _context=ctx,
        _session_id="feishu:abc",
        _memory_enabled=False,
        _working_memory=SimpleNamespace(compression_round=0, running_summary=""),
        _core_profile=None,
        _config=None,
        _summary_llm_client=None,
        _build_system_prompt=lambda: "",
    )
    activate_remote_workspace(
        session_id="feishu:abc",
        login="personal",
        requested_path="~/Project",
        ttl_seconds=None,
    )

    async def _fake_summarize(agent_obj, messages):
        return "old turns summarized"

    monkeypatch.setattr(AgentKernel, "_summarize_messages", _fake_summarize)
    summary, kept = await AgentKernel.compress_context(agent, keep_recent_turns=1)
    assert "summarized" in summary
    assert kept >= 2
    reinjected = next(
        m
        for m in ctx.messages
        if str(m.get("content", "")).startswith(WORKSPACE_STATUS_PREFIX)
    )
    body = str(reinjected.get("content"))
    assert WORKSPACE_SWITCH_PREFIX not in body
    assert "压缩" not in body
    assert "远程登录: personal" in body
    assert "授权目录:" in body
