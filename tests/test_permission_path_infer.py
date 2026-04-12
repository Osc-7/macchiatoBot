"""infer_writable_prefix_from_details。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.config import CommandToolsConfig, Config, LLMConfig
from agent_core.tools.permission_path_infer import infer_writable_prefix_from_details


@pytest.fixture
def cfg(tmp_path):
    return Config(
        llm=LLMConfig(api_key="k", model="m"),
        command_tools=CommandToolsConfig(acl_base_dir=str(tmp_path / "acl")),
    )


def test_infer_from_path_file(cfg, tmp_path):
    ctx = {"source": "feishu", "user_id": "u1"}
    d = '{"path": "/tmp/foo.txt", "reason": "t"}'
    p = infer_writable_prefix_from_details(d, config=cfg, exec_ctx=ctx)
    assert p == str(Path("/tmp").resolve())


def test_infer_from_path_prefix_key(cfg):
    ctx = {"source": "cli", "user_id": "root"}
    p = infer_writable_prefix_from_details(
        '{"path_prefix": "/var/log"}', config=cfg, exec_ctx=ctx
    )
    assert p == str(Path("/var/log").resolve())
