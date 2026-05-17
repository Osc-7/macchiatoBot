"""Tests for remote workspace path normalization."""

from agent_core.remote.pathmap import normalize_remote_workspace_relative_path


def test_normalize_relative():
    rel, err = normalize_remote_workspace_relative_path("src/a.py")
    assert err is None
    assert rel == "src/a.py"


def test_normalize_workspace_prefix():
    rel, err = normalize_remote_workspace_relative_path("/workspace/foo/bar.txt")
    assert err is None
    assert rel == "foo/bar.txt"


def test_normalize_tilde():
    rel, err = normalize_remote_workspace_relative_path("~/notes.md")
    assert err is None
    assert rel == "notes.md"


def test_reject_parent_segments():
    rel, err = normalize_remote_workspace_relative_path("../etc/passwd")
    assert rel is None
    assert err is not None


def test_reject_absolute_non_workspace():
    rel, err = normalize_remote_workspace_relative_path("/etc/passwd")
    assert rel is None
    assert err is not None
