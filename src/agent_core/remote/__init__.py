"""Remote workspace support for cloud-side AgentCore sessions."""

from .workspace_state import (
    activate_remote_workspace,
    clear_remote_workspace_state,
    format_remote_workspace_prompt_suffix,
    get_remote_workspace_skills_index,
    get_remote_workspace_state,
    release_remote_workspace,
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
from .attachment_sync import (
    format_attachment_sync_notices,
    sync_content_items_to_remote_inbox,
)

__all__ = [
    "WORKSPACE_STATUS_PREFIX",
    "WORKSPACE_SWITCH_PREFIX",
    "activate_remote_workspace",
    "append_workspace_switch_notice",
    "clear_remote_workspace_state",
    "format_attachment_sync_notices",
    "format_local_workspace_switch_notice",
    "format_remote_workspace_prompt_suffix",
    "format_remote_workspace_switch_notice",
    "get_remote_workspace_skills_index",
    "get_remote_workspace_state",
    "reinject_remote_workspace_notice_if_active",
    "release_remote_workspace",
    "sync_content_items_to_remote_inbox",
    "update_remote_workspace_skills_index",
]
