"""Remote worker file runtime path handling tests."""

from __future__ import annotations

from macchiato_remote.runtime.files import read_workspace_text, write_workspace_text


def test_absolute_path_read_is_allowed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("hello-absolute", encoding="utf-8")

    text, truncated, err = read_workspace_text(workspace, str(outside))
    assert err is None
    assert truncated is False
    assert text == "hello-absolute"


def test_absolute_path_write_is_allowed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside-write.txt"

    written, err = write_workspace_text(workspace, str(outside), "payload")
    assert err is None
    assert written > 0
    assert outside.read_text(encoding="utf-8") == "payload"

