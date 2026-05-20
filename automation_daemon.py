#!/usr/bin/env python3
"""Automation daemon compatibility shim for checkout / ``uv run``.

The implementation lives in :mod:`macchiato_bot_cli.daemon`.  A few tests and
local scripts historically imported helper functions from this root module, so
the shim keeps those imports working without restoring the old monolithic file.
"""

from macchiato_bot_cli import daemon as _daemon

get_config = _daemon.get_config
send_feishu_agent_reply = _daemon.send_feishu_agent_reply
_workspace_frontend_user_for_automation_task = (
    _daemon._workspace_frontend_user_for_automation_task
)
main = _daemon.main


async def _maybe_notify_feishu_activity(record: dict) -> None:
    """Delegate to the packaged daemon while honoring monkeypatched shim globals."""
    _daemon.get_config = get_config
    _daemon.send_feishu_agent_reply = send_feishu_agent_reply
    await _daemon._maybe_notify_feishu_activity(record)


if __name__ == "__main__":
    main()
