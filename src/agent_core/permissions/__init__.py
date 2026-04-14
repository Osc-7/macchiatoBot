"""人机权限等待与通知（request_permission / ask_user 工具）。"""

from agent_core.permissions.ask_user_registry import (
    AskUserAnswer,
    AskUserBatchDecision,
    get_ask_user_notify_hook,
    resolve_ask_user,
    set_ask_user_notify_hook,
    submit_ask_user_fragment,
)
from agent_core.permissions.wait_registry import (
    PermissionDecision,
    get_permission_notify_hook,
    register_permission_wait,
    resolve_permission,
    set_permission_notify_hook,
)

__all__ = [
    "AskUserAnswer",
    "AskUserBatchDecision",
    "PermissionDecision",
    "get_ask_user_notify_hook",
    "get_permission_notify_hook",
    "register_permission_wait",
    "resolve_ask_user",
    "resolve_permission",
    "set_ask_user_notify_hook",
    "set_permission_notify_hook",
    "submit_ask_user_fragment",
]
