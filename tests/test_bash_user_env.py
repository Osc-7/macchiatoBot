"""bash_user_env：合成「类 Linux 用户」环境与 PATH 片段。"""

import pytest

from agent_core.bash_user_env import (
    render_terminal_like_bootstrap_bash,
    validate_real_home_path_suffix,
)


def test_validate_real_home_path_suffix_ok() -> None:
    assert validate_real_home_path_suffix(".local/share/pnpm") == ".local/share/pnpm"


def test_validate_real_home_path_suffix_rejects() -> None:
    with pytest.raises(ValueError):
        validate_real_home_path_suffix("/abs")
    with pytest.raises(ValueError):
        validate_real_home_path_suffix("a/../b")


def test_render_contains_xdg_and_path_order() -> None:
    s = render_terminal_like_bootstrap_bash(
        extra_real_home_suffixes=[".local/share/pnpm"],
    )
    assert "XDG_CONFIG_HOME" in s
    assert ".local/share/pnpm" in s
    assert s.find("WORKSPACE_ROOT:-}/node_modules/.bin") < s.find(
        "MACCHIATO_PROJECT_ROOT:-}/node_modules/.bin"
    )


def test_build_guard_accepts_extra_suffixes(tmp_path) -> None:
    from agent_core.agent.workspace_paths import build_bash_workspace_guard_init

    lines = build_bash_workspace_guard_init(
        str(tmp_path / "ws"),
        project_root="/proj",
        extra_real_home_path_suffixes=["custom/bin"],
    )
    assert "custom/bin" in lines[0]
