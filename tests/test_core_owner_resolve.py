"""CorePool owner 纠正：飞书 session_id 不应落到 cli:root 日志/记忆。"""

from agent_core.agent.checkpoint import CoreCheckpoint, CoreCheckpointManager
from system.kernel.core_pool import (
    _checkpoint_owner_candidates,
    _infer_owner_from_session_id,
    _read_session_checkpoint,
    _resolve_core_owner,
)


def test_infer_feishu_user_session_id():
    assert _infer_owner_from_session_id(
        "feishu:user:ou_bc84f7f6cea2d7bdb640ae63e0fb97d3:1783431517"
    ) == ("feishu", "ou_bc84f7f6cea2d7bdb640ae63e0fb97d3")


def test_resolve_overrides_cli_default_for_feishu_session():
    assert _resolve_core_owner(
        "feishu:user:ou_abc:123",
        "cli",
        "root",
    ) == ("feishu", "ou_abc")


def test_resolve_keeps_explicit_feishu_owner():
    assert _resolve_core_owner(
        "feishu:user:ou_abc:123",
        "feishu",
        "ou_abc",
    ) == ("feishu", "ou_abc")


def test_resolve_fills_feishu_user_when_root_placeholder():
    assert _resolve_core_owner(
        "feishu:user:ou_abc:123",
        "feishu",
        "root",
    ) == ("feishu", "ou_abc")


def test_resolve_leaves_cli_session_alone():
    assert _resolve_core_owner("cli:root", "cli", "root") == ("cli", "root")


def test_checkpoint_owner_candidates_prefers_resolved_then_raw():
    sid = "feishu:user:ou_abc:123"
    assert _checkpoint_owner_candidates(sid, "cli", "root") == [
        ("feishu", "ou_abc"),
        ("cli", "root"),
    ]
    assert _checkpoint_owner_candidates("cli:root", "cli", "root") == [
        ("cli", "root"),
    ]


def test_read_session_checkpoint_falls_back_to_legacy_cli_root(tmp_path):
    """owner 纠正后仍应读到落在 cli/root 下的旧 checkpoint，避免重启丢会话。"""
    from types import SimpleNamespace

    memory_base = tmp_path / "memory"
    memory_base.mkdir()
    mem_cfg = SimpleNamespace(memory_base_dir=str(memory_base))
    cfg = SimpleNamespace(memory=mem_cfg)

    sid = "feishu:user:ou_legacy:999"
    legacy_path = memory_base / "cli" / "root" / "checkpoint.json"
    legacy_mgr = CoreCheckpointManager(str(legacy_path))
    legacy_mgr.write(
        CoreCheckpoint(
            session_id=sid,
            owner_id="root",
            source="cli",
            running_summary=None,
            recent_messages=[{"role": "user", "content": "hello"}],
            last_active_at=1_700_000_000.0,
            remaining_ttl_seconds=1800.0,
            turn_count=1,
            last_history_id=0,
            token_usage={},
        )
    )

    ckpt, mgr, legacy = _read_session_checkpoint(
        session_id=sid,
        source="cli",
        user_id="root",
        mem_cfg=mem_cfg,
        config=cfg,
    )
    assert legacy is True
    assert mgr is not None
    assert ckpt is not None
    assert ckpt.session_id == sid
    assert ckpt.recent_messages[0]["content"] == "hello"
    assert not (memory_base / "feishu" / "ou_legacy" / "checkpoint.json").exists()
