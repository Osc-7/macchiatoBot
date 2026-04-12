"""危险 bash 命令的一次性人类批准（与 request_permission 联动）。

LLM 不得自行「确认」：仅在 request_permission 被人类允许后，为本 permission_id
登记与 details.command 完全一致的命令；下一次 bash 携带相同 permission_id 与 command 时消费该许可。
"""

from __future__ import annotations

from typing import Dict, Optional

# permission_id -> 已批准、待执行的命令全文（strip 后严格相等）
_pending: Dict[str, str] = {}


def register_bash_danger_grant(permission_id: str, command: str) -> None:
    """人类批准 request_permission(bash_dangerous_command) 后登记一次性执行权。"""
    pid = (permission_id or "").strip()
    cmd = (command or "").strip()
    if not pid or not cmd:
        return
    _pending[pid] = cmd


def consume_bash_danger_grant(permission_id: Optional[str], command: str) -> bool:
    """
    若 permission_id 对应已登记的命令与 command 完全一致，则消费并返回 True。
    每个 permission_id 仅可使用一次。命令不一致时不消费，便于更正后重试。
    """
    if not permission_id:
        return False
    pid = permission_id.strip()
    cmd = command.strip()
    expected = _pending.get(pid)
    if expected is None or expected != cmd:
        return False
    _pending.pop(pid, None)
    return True


def clear_bash_danger_grant_for_tests() -> None:
    """测试用：清空待消费表。"""
    _pending.clear()
