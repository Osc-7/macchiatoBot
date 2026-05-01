"""memory_paths: 逻辑用户目录布局与命名校验。"""

from __future__ import annotations

import pytest

from agent_core.agent.memory_paths import (
    effective_memory_namespace_from_execution_context,
    ensure_memory_owner_layout,
    list_user_ids_under_frontend,
    resolve_memory_owner_paths,
    validate_logic_namespace_segment,
)
from agent_core.config import CommandToolsConfig, Config, LLMConfig, MemoryConfig


def test_validate_rejects_path_and_colon() -> None:
    with pytest.raises(ValueError):
        validate_logic_namespace_segment("../x", what="user_id")
    with pytest.raises(ValueError):
        validate_logic_namespace_segment("a:b", what="user_id")
    with pytest.raises(ValueError):
        validate_logic_namespace_segment("", what="user_id")


def test_ensure_memory_owner_layout_creates_and_idempotent(tmp_path) -> None:
    mc = MemoryConfig(memory_base_dir=str(tmp_path / "m"))
    r1 = ensure_memory_owner_layout(mc, "alice", source="cli")
    r2 = ensure_memory_owner_layout(mc, "alice", source="cli")
    assert r1["default_session_id"] == "cli:alice"
    assert r1["memory_owner"] == "cli:alice"
    assert (tmp_path / "m" / "cli" / "alice" / "long_term").is_dir()
    assert (tmp_path / "m" / "cli" / "alice" / "content").is_dir()
    assert len(r1["created_paths"]) >= 1
    assert r2["created_paths"] == []


def test_effective_memory_namespace_prefers_memory_owner() -> None:
    fe, uid = effective_memory_namespace_from_execution_context(
        {
            "memory_owner": "feishu:ou_abc",
            "source": "cron",
            "user_id": "default",
        }
    )
    assert fe == "feishu"
    assert uid == "ou_abc"


def test_effective_memory_namespace_fallback_source_user() -> None:
    fe, uid = effective_memory_namespace_from_execution_context(
        {"source": "cli", "user_id": "root"}
    )
    assert fe == "cli"
    assert uid == "root"


def test_list_user_ids_under_frontend(tmp_path) -> None:
    mc = MemoryConfig(memory_base_dir=str(tmp_path / "m"))
    ensure_memory_owner_layout(mc, "u1", source="cli")
    ensure_memory_owner_layout(mc, "u2", source="cli")
    ids = list_user_ids_under_frontend(mc, frontend="cli")
    assert set(ids) == {"u1", "u2"}


def test_resolve_memory_owner_paths_uses_linux_home_for_tenant(tmp_path) -> None:
    cfg = Config(
        llm=LLMConfig(api_key="t", model="t"),
        memory=MemoryConfig(memory_base_dir=str(tmp_path / "legacy-mem")),
        command_tools=CommandToolsConfig(
            bash_os_user_enabled=True,
            bash_os_user_home_base_dir=str(tmp_path / "homes"),
        ),
    )
    paths = resolve_memory_owner_paths(cfg.memory, "u1", config=cfg, source="feishu")
    assert paths["memory_md_path"] == str(
        tmp_path / "homes" / "m_feishu_u1" / "data" / "memory" / "long_term" / "MEMORY.md"
    )
