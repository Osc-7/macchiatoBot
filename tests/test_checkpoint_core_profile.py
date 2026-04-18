"""CoreProfile 在 checkpoint 中的序列化与反序列化。"""

from agent_core.kernel_interface import (
    CoreProfile,
    core_profile_from_checkpoint_dict,
    core_profile_to_checkpoint_dict,
)


def test_core_profile_checkpoint_roundtrip() -> None:
    p = CoreProfile.for_shuiyuan(dialog_window_id="Osc7")
    d = core_profile_to_checkpoint_dict(p)
    assert d is not None
    assert d.get("tool_template") == "shuiyuan"
    assert d.get("frontend_id") == "shuiyuan"
    assert d.get("dialog_window_id") == "Osc7"
    p2 = core_profile_from_checkpoint_dict(d)
    assert p2 is not None
    assert p2.tool_template == p.tool_template
    assert p2.tool_exposure_mode == p.tool_exposure_mode
    assert p2.frontend_id == p.frontend_id
    assert p2.dialog_window_id == p.dialog_window_id
    assert p2.mode == p.mode


def test_core_profile_checkpoint_none() -> None:
    assert core_profile_to_checkpoint_dict(None) is None
    assert core_profile_from_checkpoint_dict(None) is None
    assert core_profile_from_checkpoint_dict({}) is None
