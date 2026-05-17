from __future__ import annotations

from frontend.feishu.ipc_bridge import (
    _default_feishu_session_id,
    _extract_override_session_target,
)
from frontend.feishu.session_override import (
    resolve_session_override,
    set_session_override,
)


def test_session_override_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHEDULE_AGENT_TEST_DATA_DIR", str(tmp_path))
    set_session_override(
        chat_type="p2p",
        chat_id="oc_xxx",
        open_id="ou_abc",
        user_id="u_abc",
        session_id="feishu:custom:1",
    )
    sid = resolve_session_override(
        chat_type="p2p",
        chat_id="oc_xxx",
        open_id="ou_abc",
        user_id="u_abc",
    )
    assert sid == "feishu:custom:1"


def test_extract_override_target():
    assert _extract_override_session_target("/session switch a:b") == ("a:b", False)
    assert _extract_override_session_target("/session new") == (None, True)
    assert _extract_override_session_target("/new") == (None, True)
    assert _extract_override_session_target("/new my-session") == ("my-session", False)
    assert _extract_override_session_target("/help") == (None, False)


def test_default_feishu_session_id_matches_mapping_shape():
    assert (
        _default_feishu_session_id(
            chat_type="p2p",
            chat_id="oc_xxx",
            open_id="ou_abc",
            user_id="u_abc",
        )
        == "feishu:user:ou_abc"
    )
    assert (
        _default_feishu_session_id(
            chat_type="group",
            chat_id="oc_group",
            open_id="ou_abc",
            user_id="u_abc",
        )
        == "feishu:chat:oc_group"
    )
