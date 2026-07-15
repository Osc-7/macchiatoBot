"""Remote workspace support for cloud-side AgentCore sessions."""

from .workspace_state import (
    activate_remote_workspace,
    clear_remote_workspace_state,
    format_remote_workspace_prompt_suffix,
    get_remote_workspace_skills_index,
    get_remote_workspace_state,
    release_remote_workspace,
    remote_ttl_lapsed,
    REMOTE_TTL_EXPIRED_MESSAGE,
    update_remote_workspace_skills_index,
)
from .workspace_notice import (
    WORKSPACE_STATUS_PREFIX,
    WORKSPACE_SWITCH_PREFIX,
    append_workspace_switch_notice,
    format_local_workspace_switch_notice,
    format_remote_workspace_switch_notice,
    reinject_remote_workspace_notice_if_active,
)

__all__ = [
    "WORKSPACE_STATUS_PREFIX",
    "WORKSPACE_SWITCH_PREFIX",
    "activate_remote_workspace",
    "append_workspace_switch_notice",
    "clear_remote_workspace_state",
    "format_local_workspace_switch_notice",
    "format_remote_workspace_prompt_suffix",
    "format_remote_workspace_switch_notice",
    "get_remote_workspace_skills_index",
    "get_remote_workspace_state",
    "reinject_remote_workspace_notice_if_active",
    "release_remote_workspace",
    "remote_ttl_lapsed",
    "REMOTE_TTL_EXPIRED_MESSAGE",
    "update_remote_workspace_skills_index",
]
