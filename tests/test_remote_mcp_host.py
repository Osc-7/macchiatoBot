"""Workspace MCP config + host unit tests (no real MCP SDK required for config)."""

from pathlib import Path

from macchiato_remote.runtime.macchiato_dir import ensure_macchiato_layout
from macchiato_remote.runtime.mcp_config import (
    MCP_YAML_REL,
    find_server,
    load_workspace_mcp_config,
)
from macchiato_remote.runtime.mcp_host import RemoteMcpHost
import pytest


def test_ensure_layout_writes_mcp_yaml_template(tmp_path: Path):
    ensure_macchiato_layout(tmp_path, device_label="test")
    yaml_path = tmp_path / MCP_YAML_REL
    assert yaml_path.is_file()
    text = yaml_path.read_text(encoding="utf-8")
    assert "mcp:" in text
    assert "servers:" in text


def test_load_workspace_mcp_config(tmp_path: Path):
    ensure_macchiato_layout(tmp_path)
    yaml_path = tmp_path / MCP_YAML_REL
    yaml_path.write_text(
        """
mcp:
  servers:
    - name: local_chrome
      enabled: true
      command: npx
      args: ["-y", "chrome-devtools-mcp@latest"]
""",
        encoding="utf-8",
    )
    cfg = load_workspace_mcp_config(tmp_path)
    srv = find_server(cfg, "local_chrome")
    assert srv is not None
    assert srv.command == "npx"
    assert find_server(cfg, "missing") is None


@pytest.mark.asyncio
async def test_mcp_host_ensure_missing_sdk_or_config(tmp_path: Path):
    host = RemoteMcpHost()
    host.bind_workspace("sess-1", tmp_path)
    # no mcp.yaml yet
    rows = await host.ensure("sess-1", ["chrome"])
    assert rows[0]["ok"] is False
    assert rows[0]["error"] in {
        "MCP_CONFIG_MISSING",
        "MCP_SDK_MISSING",
        "SESSION_NOT_OPEN",
    }

    ensure_macchiato_layout(tmp_path)
    (tmp_path / MCP_YAML_REL).write_text(
        "mcp:\n  servers:\n    - name: chrome\n      command: /bin/false\n",
        encoding="utf-8",
    )
    rows2 = await host.ensure("sess-1", ["chrome"])
    # Either SDK missing in env, or connect fails — must not crash
    assert rows2[0]["name"] == "chrome"
    assert "ok" in rows2[0]
