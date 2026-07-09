"""Tests for runtime provider CLI/YAML parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.llm.provider_runtime import (
    load_providers_from_yaml,
    parse_model_add_cli,
)


def test_load_providers_from_yaml_single_entry(tmp_path: Path) -> None:
    p = tmp_path / "glm.yaml"
    p.write_text(
        """
glm52:
  label: "GLM-5.2"
  base_url: "https://glm.example/v4"
  api_key: "${GLM_API_KEY}"
  model: "glm-5.2"
  capabilities:
    reasoning_content: true
""",
        encoding="utf-8",
    )
    loaded = load_providers_from_yaml(p)
    assert "glm52" in loaded
    assert loaded["glm52"]["base_url"] == "https://glm.example/v4"
    assert loaded["glm52"]["model"] == "glm-5.2"


def test_parse_model_add_cli_inline_flags() -> None:
    req = parse_model_add_cli(
        'add my_glm --base-url https://glm/v4 --api-key sk-test --model glm-5.2 '
        '--label "GLM 5.2" --reasoning --switch'
    )
    assert len(req.entries) == 1
    e = req.entries[0]
    assert e.name == "my_glm"
    assert e.provider["base_url"] == "https://glm/v4"
    assert e.provider["api_key"] == "sk-test"
    assert e.provider["model"] == "glm-5.2"
    assert e.provider["label"] == "GLM 5.2"
    assert e.provider["capabilities"]["reasoning_content"] is True
    assert req.switch_to == "my_glm"


def test_parse_model_add_cli_from_file(tmp_path: Path) -> None:
    p = tmp_path / "one.yaml"
    p.write_text(
        """
solo:
  base_url: "https://a/v1"
  api_key: "k"
  model: "m1"
""",
        encoding="utf-8",
    )
    req = parse_model_add_cli(f"add --file {p}")
    assert len(req.entries) == 1
    assert req.entries[0].name == "solo"
    assert req.entries[0].provider["model"] == "m1"


def test_parse_model_add_cli_file_with_alias(tmp_path: Path) -> None:
    p = tmp_path / "one.yaml"
    p.write_text(
        """
orig:
  base_url: "https://a/v1"
  api_key: "k"
  model: "m1"
""",
        encoding="utf-8",
    )
    req = parse_model_add_cli(f"add alias --file {p} --api-key override-key")
    assert req.entries[0].name == "alias"
    assert req.entries[0].provider["api_key"] == "override-key"


def test_parse_model_add_cli_json_tail() -> None:
    req = parse_model_add_cli(
        'add x -u https://a/v1 -k k -m m '
        '\'{"vendor_params":{"enable_thinking":true}}\''
    )
    assert req.entries[0].provider["vendor_params"]["enable_thinking"] is True


def test_parse_model_add_cli_requires_base_url_when_inline() -> None:
    with pytest.raises(ValueError, match="base-url"):
        parse_model_add_cli("add onlyname --api-key k")
