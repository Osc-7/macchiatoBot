"""Load workspace-local MCP server definitions from ``.macchiato/mcp.yaml``.

Keep this module free of ``agent_core`` imports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

MCP_YAML_REL = ".macchiato/mcp.yaml"

_MCP_YAML_TEMPLATE = """# .macchiato/mcp.yaml — MCP servers started on this workspace's machine
# Names must match daemon config entries with location: remote.
# env is optional (only when the MCP process itself needs API keys).
mcp:
  servers: []
  # - name: example
  #   enabled: true
  #   transport: stdio
  #   command: npx
  #   args: ["-y", "some-mcp@latest"]
  #   cwd: null
"""


class WorkspaceMcpServerConfig(BaseModel):
    """One stdio MCP server declared under a workspace ``.macchiato/mcp.yaml``."""

    name: str
    enabled: bool = True
    transport: str = "stdio"
    command: str
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    cwd: Optional[str] = None
    init_timeout_seconds: int = Field(default=20, ge=1)
    call_timeout_seconds: int = Field(default=45, ge=1)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class WorkspaceMcpConfig(BaseModel):
    servers: List[WorkspaceMcpServerConfig] = Field(default_factory=list)


class WorkspaceMcpFile(BaseModel):
    mcp: WorkspaceMcpConfig = Field(default_factory=WorkspaceMcpConfig)


def mcp_yaml_path(workspace_root: Path | str) -> Path:
    return Path(workspace_root).expanduser().resolve() / MCP_YAML_REL


def ensure_mcp_yaml_template(workspace_root: Path | str) -> Optional[str]:
    """Create an empty commented template if missing. Returns created path or None."""
    path = mcp_yaml_path(workspace_root)
    if path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_MCP_YAML_TEMPLATE, encoding="utf-8")
    return str(path)


def load_workspace_mcp_config(workspace_root: Path | str) -> WorkspaceMcpConfig:
    """Load ``.macchiato/mcp.yaml``; missing file yields empty servers list."""
    path = mcp_yaml_path(workspace_root)
    if not path.is_file():
        return WorkspaceMcpConfig()
    text = path.read_text(encoding="utf-8")
    try:
        import yaml

        data = yaml.safe_load(text) or {}
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required to parse .macchiato/mcp.yaml; "
            "install macchiato-remote[mcp]"
        ) from exc
    if not isinstance(data, dict):
        return WorkspaceMcpConfig()
    return WorkspaceMcpFile.model_validate(data).mcp


def find_server(
    config: WorkspaceMcpConfig, name: str
) -> Optional[WorkspaceMcpServerConfig]:
    key = (name or "").strip()
    for server in config.servers:
        if server.name == key:
            return server
    return None
