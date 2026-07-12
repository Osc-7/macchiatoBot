"""Tests for the canonical ``.macchiato/`` workspace layout."""

from __future__ import annotations

from pathlib import Path

from agent_core.agent.workspace_paths import (
    build_bash_workspace_guard_init,
    ensure_workspace_owner_layout,
)
from agent_core.config import CommandToolsConfig
from macchiato_remote.runtime.macchiato_dir import (
    DEVICE_MD_REL,
    JOURNAL_REL,
    JOBS_REL,
    ensure_macchiato_layout,
    resolve_macchiato_paths,
)
from macchiato_remote.runtime.workspace_guard import build_remote_workspace_guard_init


def test_ensure_macchiato_layout_creates_tree_and_device_md(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    r1 = ensure_macchiato_layout(root, device_label="studio")
    assert (root / ".macchiato" / "jobs").is_dir()
    assert (root / ".macchiato" / "journal").is_dir()
    assert (root / ".macchiato" / "rules").is_dir()
    assert (root / ".macchiato" / "skills").is_dir()
    assert (root / ".macchiato" / "scratch").is_dir()
    device = root / ".macchiato" / "DEVICE.md"
    assert device.is_file()
    assert "studio" in device.read_text(encoding="utf-8")
    assert JOURNAL_REL in device.read_text(encoding="utf-8")
    assert r1["journal_dir"] == str(root / ".macchiato" / "journal")
    assert DEVICE_MD_REL.endswith("DEVICE.md")

    # Idempotent: no overwrite of DEVICE.md, no duplicate creates.
    device.write_text("custom\n", encoding="utf-8")
    r2 = ensure_macchiato_layout(root, device_label="other")
    assert device.read_text(encoding="utf-8") == "custom\n"
    assert r2["created_paths"] == []


def test_resolve_macchiato_paths(tmp_path: Path) -> None:
    paths = resolve_macchiato_paths(tmp_path)
    assert paths["jobs_dir"] == str(tmp_path.resolve() / JOBS_REL)
    assert paths["macchiato_dir"] == str(tmp_path.resolve() / ".macchiato")


def test_ensure_workspace_owner_layout_creates_macchiato(tmp_path: Path) -> None:
    cfg = CommandToolsConfig(workspace_base_dir=str(tmp_path / "w"))
    result = ensure_workspace_owner_layout(cfg, "alice", source="cli")
    owner = Path(result["owner_dir"])
    assert (owner / ".macchiato" / "journal").is_dir()
    assert (owner / ".macchiato" / "DEVICE.md").is_file()
    assert result["macchiato"]["journal_dir"] == str(owner / ".macchiato" / "journal")


def test_bash_guard_exports_macchiato_dir(tmp_path: Path) -> None:
    root = str(tmp_path / "ws")
    script = build_bash_workspace_guard_init(root, project_root="/proj")[0]
    assert 'export MACCHIATO_DIR="$MACCHIATO_WORKSPACE_ROOT/.macchiato"' in script
    assert "$MACCHIATO_DIR/journal" in script


def test_remote_guard_exports_macchiato_dir(tmp_path: Path) -> None:
    script = build_remote_workspace_guard_init(tmp_path)
    assert "MACCHIATO_DIR=" in script
    assert ".macchiato/journal" in script or "$MACCHIATO_DIR/journal" in script
