"""memory_paths: 逻辑用户目录布局与命名校验。"""

from __future__ import annotations

import pytest

from agent_core.agent.memory_paths import (
    ensure_memory_owner_layout,
    list_user_ids_under_frontend,
    validate_logic_namespace_segment,
)
from agent_core.config import MemoryConfig


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


def test_list_user_ids_under_frontend(tmp_path) -> None:
    mc = MemoryConfig(memory_base_dir=str(tmp_path / "m"))
    ensure_memory_owner_layout(mc, "u1", source="cli")
    ensure_memory_owner_layout(mc, "u2", source="cli")
    ids = list_user_ids_under_frontend(mc, frontend="cli")
    assert set(ids) == {"u1", "u2"}
