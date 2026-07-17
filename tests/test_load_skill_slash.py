"""Gateway /skill path: force load_skill into session context."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_core.context.conversation import ConversationContext
from agent_core.tools.base import ToolResult
from system.automation.core_gateway import AutomationCoreGateway


@pytest.mark.asyncio
async def test_load_skill_for_session_injects_tool_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = ConversationContext()
    agent = MagicMock()
    agent.config = MagicMock()
    agent._context = ctx
    agent._core_profile = None
    agent._finalize_turn = AsyncMock()

    async def _fake_execute(**kwargs):
        assert kwargs.get("skill_name") == "demo-skill"
        assert kwargs["__execution_context__"]["session_id"] == "cli:root"
        return ToolResult(
            success=True,
            data={"skill_name": "demo-skill", "content": "# Demo\n"},
            message="Loaded skill `demo-skill`.\n\n---\n# Demo\n",
            metadata={"workspace_backend": "local"},
        )

    fake_tool = MagicMock()
    fake_tool.execute = AsyncMock(side_effect=_fake_execute)
    monkeypatch.setattr(
        "system.tools.load_skill_tool.LoadSkillTool",
        lambda _config: fake_tool,
    )

    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=agent)
    scheduler = MagicMock()
    scheduler.core_pool = pool

    class _Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    scheduler.hold_session_lock = MagicMock(return_value=_Lock())

    gw = AutomationCoreGateway(
        MagicMock(),
        kernel_scheduler=scheduler,
        session_id="cli:root",
        owner_id="root",
        source="cli",
        session_registry=MagicMock(),
    )
    gw.ensure_session = AsyncMock()

    out = await gw.load_skill_for_session("demo-skill", session_id="cli:root")

    assert out["ok"] is True
    assert out["injected"] is True
    assert out["backend"] == "local"
    msgs = ctx.get_messages()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["tool_calls"][0]["function"]["name"] == "load_skill"
    assert msgs[1]["role"] == "tool"
    agent._finalize_turn.assert_awaited_once_with(None)
