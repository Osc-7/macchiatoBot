"""writable_roots_store ACL 持久化。"""

from __future__ import annotations

import json

from agent_core.agent.writable_roots_store import (
    append_user_writable_prefix,
    load_user_writable_prefixes,
)


def test_load_empty(tmp_path):
    assert load_user_writable_prefixes(str(tmp_path / "acl"), "cli", "u1") == []


def test_append_and_load_idempotent(tmp_path):
    base = str(tmp_path / "acl")
    append_user_writable_prefix(base, "feishu", "ou_x", str(tmp_path / "a"))
    append_user_writable_prefix(base, "feishu", "ou_x", str(tmp_path / "a"))
    p = load_user_writable_prefixes(base, "feishu", "ou_x")
    assert len(p) == 1
    assert p[0] == str((tmp_path / "a").resolve())
    f = tmp_path / "acl" / "feishu" / "ou_x" / "writable_roots.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    assert len(data["prefixes"]) == 1
