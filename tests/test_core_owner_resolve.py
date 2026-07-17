"""CorePool owner 纠正：飞书 session_id 不应落到 cli:root 日志/记忆。"""

from system.kernel.core_pool import _infer_owner_from_session_id, _resolve_core_owner


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
