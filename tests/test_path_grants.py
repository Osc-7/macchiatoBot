"""path_grants: 统一 read/write grant 接口。"""

from __future__ import annotations

import json

from agent_core.agent.path_grants import (
    add_ephemeral_path_prefix,
    append_user_path_prefix,
    clear_ephemeral_path_grants_for_tests,
    list_ephemeral_path_prefixes,
    load_user_path_prefixes,
)


def test_append_and_load_persisted_read_grant(tmp_path) -> None:
    base = str(tmp_path / "acl")
    target = tmp_path / "shared-read"
    append_user_path_prefix(
        base,
        "feishu",
        "ou_x",
        str(target),
        access_mode="read",
    )
    append_user_path_prefix(
        base,
        "feishu",
        "ou_x",
        str(target),
        access_mode="read",
    )
    grants = load_user_path_prefixes(base, "feishu", "ou_x", access_mode="read")
    assert grants == [str(target.resolve())]
    data = json.loads(
        (tmp_path / "acl" / "feishu" / "ou_x" / "readable_roots.json").read_text(
            encoding="utf-8"
        )
    )
    assert data["prefixes"] == [str(target.resolve())]


def test_append_and_load_persisted_write_grant(tmp_path) -> None:
    base = str(tmp_path / "acl")
    target = tmp_path / "shared-write"
    append_user_path_prefix(
        base,
        "cli",
        "alice",
        str(target),
        access_mode="write",
    )
    grants = load_user_path_prefixes(base, "cli", "alice", access_mode="write")
    assert grants == [str(target.resolve())]
    data = json.loads(
        (tmp_path / "acl" / "cli" / "alice" / "writable_roots.json").read_text(
            encoding="utf-8"
        )
    )
    assert data["prefixes"] == [str(target.resolve())]


def test_ephemeral_grants_are_scoped_by_access_mode(tmp_path) -> None:
    clear_ephemeral_path_grants_for_tests()
    read_target = tmp_path / "read-once"
    write_target = tmp_path / "write-once"
    add_ephemeral_path_prefix(
        "cli",
        "alice",
        str(read_target),
        access_mode="read",
    )
    add_ephemeral_path_prefix(
        "cli",
        "alice",
        str(write_target),
        access_mode="write",
    )

    assert list_ephemeral_path_prefixes("cli", "alice", access_mode="read") == [
        str(read_target.resolve())
    ]
    assert list_ephemeral_path_prefixes("cli", "alice", access_mode="write") == [
        str(write_target.resolve())
    ]

    clear_ephemeral_path_grants_for_tests(access_mode="read")
    assert list_ephemeral_path_prefixes("cli", "alice", access_mode="read") == []
    assert list_ephemeral_path_prefixes("cli", "alice", access_mode="write") == [
        str(write_target.resolve())
    ]
    clear_ephemeral_path_grants_for_tests()
