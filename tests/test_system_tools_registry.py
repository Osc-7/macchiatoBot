"""Tests for system.tools.build_tool_registry."""

from __future__ import annotations

from pathlib import Path

from agent_core.config import AgentConfig, Config, LLMConfig, MemoryConfig
from agent_core.kernel_interface import CoreProfile
from system.tools import VersionedToolRegistry, build_tool_registry


def _isolated_registry_config(tmp_path: Path) -> Config:
    """避免单测依赖进程全局 ``get_config()`` 中的磁盘路径（CI 上可能不可写）。"""
    return Config(
        llm=LLMConfig(api_key="k", model="test-model"),
        memory=MemoryConfig(memory_base_dir=str(tmp_path / "memory")),
        agent=AgentConfig(),
    )


def test_build_tool_registry_returns_registry(tmp_path: Path) -> None:
    profile = CoreProfile.default_full()
    registry = build_tool_registry(
        profile=profile, config=_isolated_registry_config(tmp_path)
    )
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
    # call_tool 查的是同一 registry，须能解析 request_permission / ask_user（与 AgentCore 自注册一致）
    assert "request_permission" in names
    assert "ask_user" in names


def test_build_tool_registry_respects_profile_allowlist(tmp_path: Path) -> None:
    profile = CoreProfile(
        mode="sub",
        allowed_tools=["parse_time", "get_events"],
        deny_tools=[],
        allow_dangerous_commands=False,
    )
    registry = build_tool_registry(
        profile=profile, config=_isolated_registry_config(tmp_path)
    )
    names = set(registry.list_names())

    assert "parse_time" in names
    assert "get_events" in names
    # 未在白名单中的 schedule 工具应被过滤掉
    assert "add_event" not in names
    assert "add_task" not in names
    # bash 由 AgentCore.__aenter__ 运行时自注册；build_tool_registry 只验证静态业务工具过滤
    assert "bash" not in names
    assert "run_command" not in names
