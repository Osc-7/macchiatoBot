"""Tests for system.tools.build_tool_registry."""

from __future__ import annotations

from agent_core.kernel_interface import CoreProfile
from system.tools import VersionedToolRegistry, build_tool_registry


def test_build_tool_registry_returns_registry() -> None:
    profile = CoreProfile.default_full()
    registry = build_tool_registry(profile=profile)
    assert isinstance(registry, VersionedToolRegistry)

    names = set(registry.list_names())
    # schedule 核心工具应始终存在
    assert "parse_time" in names
    assert "add_event" in names
    assert "add_task" in names
    assert "get_events" in names
    assert "get_tasks" in names
    assert "get_free_slots" in names
    assert "plan_tasks" in names


def test_build_tool_registry_respects_profile_allowlist() -> None:
    profile = CoreProfile(
        mode="sub",
        allowed_tools=["parse_time", "get_events"],
        deny_tools=[],
        allow_dangerous_commands=False,
    )
    registry = build_tool_registry(profile=profile)
    names = set(registry.list_names())

    assert "parse_time" in names
    assert "get_events" in names
    # 未在白名单中的 schedule 工具应被过滤掉
    assert "add_event" not in names
    assert "add_task" not in names
    # bash 由 AgentCore.__aenter__ 运行时自注册；build_tool_registry 只验证静态业务工具过滤
    assert "bash" not in names
    assert "run_command" not in names
