"""人机权限等待与通知（request_permission 工具）。"""

from agent_core.permissions.wait_registry import (
    PermissionDecision,
    get_permission_notify_hook,
    register_permission_wait,
    resolve_permission,
    set_permission_notify_hook,
)

__all__ = [
    "PermissionDecision",
    "get_permission_notify_hook",
    "register_permission_wait",
    "resolve_permission",
    "set_permission_notify_hook",
]
