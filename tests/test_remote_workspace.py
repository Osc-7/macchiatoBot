from __future__ import annotations

import pytest

from agent_core.remote.workspace_state import (
    activate_remote_workspace,
    clear_remote_workspace_state,
    format_remote_workspace_prompt_suffix,
    get_remote_workspace_state,
    release_remote_workspace,
)
from macchiato_remote.protocol import REMOTE_WORKSPACE_MOUNT, RemoteWorkspaceState


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


def test_remote_prompt_suffix_describes_backend_switch():
    state = activate_remote_workspace(
        session_id="feishu:abc",
        login="personal",
        requested_path="~/Project",
        profile="dev",
        ttl_seconds=None,
    )

    suffix = format_remote_workspace_prompt_suffix(state)

    assert "当前远程工作区模式" in suffix
    assert "远程登录: personal" in suffix
    assert "/workspace" in suffix
    assert "不要假设云服务器项目目录" in suffix
