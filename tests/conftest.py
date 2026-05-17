"""Pytest 插件：隔离可写 HOME / 临时根目录，并恢复被单测污染的 LLMClient 类属性。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.llm.client import LLMClient

_LLM_DESCRIPTOR_KEYS = ("context_window", "max_tokens", "capabilities", "model")
_LLM_CLASS_SNAPSHOT: dict[str, object] = {
    k: LLMClient.__dict__[k]
    for k in _LLM_DESCRIPTOR_KEYS
    if k in LLMClient.__dict__
}


@pytest.fixture(autouse=True)
def _pytest_isolation_env_and_llm_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """避免 CI / 沙箱中不可写的 HOME、/tmp/macchiato，并防止 test_agent 污染 LLMClient。"""
    home = tmp_path / "pytest_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    mt = tmp_path / "pytest_macchiato_tmp"
    mt.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MACCHIATO_TMP_BASE", str(mt))

    yield

    for key, desc in _LLM_CLASS_SNAPSHOT.items():
        setattr(LLMClient, key, desc)
