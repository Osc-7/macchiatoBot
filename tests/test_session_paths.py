"""session_paths：主进程 ~ 与会话工作区对齐。"""

from pathlib import Path

import pytest

from agent_core.config import CommandToolsConfig, Config, LLMConfig
from agent_core.agent.session_paths import expand_user_path_str_for_session


@pytest.fixture
def isolated_config(tmp_path) -> Config:
    return Config(
        llm=LLMConfig(api_key="k", model="m"),
        command_tools=CommandToolsConfig(
            base_dir=str(tmp_path),
            workspace_base_dir=str(tmp_path / "ws"),
            workspace_isolation_enabled=True,
        ),
    )


def test_tilde_maps_to_user_cell_when_isolated(isolated_config: Config, tmp_path) -> None:
    ctx = {"source": "feishu", "user_id": "u1", "bash_workspace_admin": False}
    out = expand_user_path_str_for_session(
        "~/.agents/foo.txt", isolated_config, exec_ctx=ctx
    )
    assert ".agents" in out and "feishu" in out and "u1" in out
    assert Path(out).is_absolute()


def test_tilde_uses_os_home_when_no_isolation(tmp_path) -> None:
    cfg = Config(
        llm=LLMConfig(api_key="k", model="m"),
        command_tools=CommandToolsConfig(
            base_dir=str(tmp_path),
            workspace_isolation_enabled=False,
        ),
    )
    out = expand_user_path_str_for_session("~/x", cfg, exec_ctx={"source": "cli", "user_id": "root"})
    assert Path(out).resolve() == (Path.home() / "x").resolve()
