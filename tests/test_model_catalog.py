"""
ModelCatalog：运行时注册 / 磁盘重载 / 存活 LLMClient 增量同步 / 文件指纹。

daemon 是模型目录的唯一权威；这里验证目录变更如何进入已构造的 LLMClient，
以及 config.yaml + providers.d + _runtime.yaml 的重载 diff。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agent_core.config import (
    CapabilitiesModel,
    Config,
    LLMConfig,
    ProviderEntry,
    load_config,
    reset_config,
)
from agent_core.llm import LLMClient
from agent_core.llm.catalog import (
    ModelCatalog,
    catalog_files_fingerprint,
    get_model_catalog,
    reset_model_catalog,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    reset_model_catalog()
    yield
    reset_config()
    reset_model_catalog()


def _make_config() -> Config:
    return Config(
        llm=LLMConfig(
            providers={
                "deepseek": ProviderEntry(
                    base_url="https://example.com/v1",
                    api_key="k",
                    model="deepseek-chat",
                    capabilities=CapabilitiesModel(vision=False),
                ),
                "qwen3vl": ProviderEntry(
                    base_url="https://example.com/v1",
                    api_key="k",
                    model="qwen",
                    capabilities=CapabilitiesModel(vision=True),
                ),
            },
            active="deepseek",
        )
    )


def _write_provider_file(
    dir_: Path, name: str, model: str, *, vision: bool = False
) -> Path:
    p = dir_ / f"{name}.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                name: {
                    "base_url": "https://example.com/v1",
                    "api_key": "k",
                    "model": model,
                    "capabilities": {"vision": vision},
                }
            }
        ),
        encoding="utf-8",
    )
    return p


def _make_disk_config(tmp_path: Path) -> tuple[Path, Path]:
    cfg_dir = tmp_path / "config"
    prov_dir = cfg_dir / "llm" / "providers.d"
    prov_dir.mkdir(parents=True)
    main = cfg_dir / "config.yaml"
    main.write_text(
        "llm:\n  active: a\n  providers_dir: llm/providers.d\n  providers:\n"
        "    a:\n      base_url: https://a/v1\n      api_key: k\n      model: ma\n",
        encoding="utf-8",
    )
    return main, prov_dir


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def test_register_updates_config_and_notifies_live_client():
    cfg = _make_config()
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI"):
        client = LLMClient(config=cfg)
    catalog = get_model_catalog()
    assert catalog.attached_client_count == 1

    with patch("agent_core.config.persist_runtime_provider") as persist:
        pe = catalog.register(
            "glm52",
            {"base_url": "https://glm/v1", "api_key": "k", "model": "glm-5.2"},
            persist=True,
            config=cfg,
        )
    assert pe.model == "glm-5.2"
    assert "glm52" in cfg.llm.providers
    # 存活 client 已通过 weakref 通知增量同步
    assert "glm52" in client.providers
    assert catalog.generation == 1
    persist.assert_called_once()
    client.switch_model("glm52")
    assert client.active_provider_name == "glm52"


def test_register_duplicate_requires_overwrite():
    cfg = _make_config()
    catalog = ModelCatalog()
    entry = {"base_url": "https://x/v1", "api_key": "k", "model": "m1"}
    catalog.register("x", entry, persist=False, config=cfg)
    with pytest.raises(ValueError):
        catalog.register("x", entry, persist=False, config=cfg)
    catalog.register(
        "x", {**entry, "model": "m2"}, persist=False, overwrite=True, config=cfg
    )
    assert cfg.llm.providers["x"].model == "m2"


def test_register_expands_env_vars(monkeypatch):
    monkeypatch.setenv("TEST_CATALOG_KEY", "sk-from-env")
    cfg = _make_config()
    pe = ModelCatalog().register(
        "envp",
        {"base_url": "https://x/v1", "api_key": "${TEST_CATALOG_KEY}", "model": "m"},
        persist=False,
        config=cfg,
    )
    assert pe.api_key == "sk-from-env"


def test_register_rejects_invalid_entry():
    cfg = _make_config()
    with pytest.raises(Exception):
        ModelCatalog().register("bad", {"api_key": "k"}, persist=False, config=cfg)
    assert "bad" not in cfg.llm.providers


def test_snapshot_rows_match_list_models_shape():
    cfg = _make_config()
    rows = ModelCatalog().snapshot(config=cfg)
    names = [r["name"] for r in rows]
    assert names == ["deepseek", "qwen3vl"]
    by = {r["name"]: r for r in rows}
    assert by["deepseek"]["is_active"] is True
    assert by["qwen3vl"]["is_vision_provider"] is True
    assert by["qwen3vl"]["api_model"] == "qwen"
    assert {
        "name",
        "model",
        "api_model",
        "label",
        "base_url",
        "vision",
        "function_calling",
        "reasoning_content",
        "context_window",
        "is_active",
        "is_vision_provider",
    } <= set(by["deepseek"])


# ---------------------------------------------------------------------------
# reload_from_disk
# ---------------------------------------------------------------------------


def test_reload_from_disk_diff_and_live_sync(tmp_path, monkeypatch):
    main, prov_dir = _make_disk_config(tmp_path)
    _write_provider_file(prov_dir, "b", "mb")
    monkeypatch.chdir(tmp_path)
    cfg = load_config(main)
    assert set(cfg.llm.providers) == {"a", "b"}

    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI"):
        client = LLMClient(config=cfg)
    assert client.active_provider_name == "a"

    # 新增 c、修改 b、删除 a（a 是 active）
    _write_provider_file(prov_dir, "c", "mc", vision=True)
    _write_provider_file(prov_dir, "b", "mb-v2")
    main.write_text(
        "llm:\n  active: b\n  providers_dir: llm/providers.d\n",
        encoding="utf-8",
    )

    catalog = get_model_catalog()
    result = catalog.reload_from_disk(config=cfg, config_path=main)
    assert result["added"] == ["c"]
    assert result["removed"] == ["a"]
    assert result["changed"] == ["b"]
    assert result["generation"] == 1
    assert result["active"] == "b"
    assert result["vision_provider"] == "c"

    # 全局 config 原地更新
    assert set(cfg.llm.providers) == {"b", "c"}
    assert cfg.llm.providers["b"].model == "mb-v2"
    # 存活 client：新增可见、变更重建、active 被删仍保留旧实例
    assert "c" in client.providers
    assert client.providers["b"].model == "mb-v2"
    assert client.active_provider_name == "a"
    assert "a" in client.providers
    assert client.vision_provider_name == "c"
    # 切走之后再同步，旧 active 才被清理
    client.switch_model("b")
    client.sync_from_catalog()
    assert "a" not in client.providers


def test_reload_from_disk_rejects_empty_and_keeps_previous(tmp_path, monkeypatch):
    main, _prov_dir = _make_disk_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = load_config(main)
    # 主配置损坏：load_config 抛错，目录保持上一代
    main.write_text("llm: [not a mapping]\n", encoding="utf-8")
    with pytest.raises(Exception):
        get_model_catalog().reload_from_disk(config=cfg, config_path=main)
    assert set(cfg.llm.providers) == {"a"}
    assert get_model_catalog().generation == 0


def test_runtime_yaml_is_picked_up_by_reload(tmp_path, monkeypatch):
    """/model add 持久化的 _runtime.yaml 在 daemon 重启 / reload 后仍可见。"""
    from agent_core.config import persist_runtime_provider

    main, prov_dir = _make_disk_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = load_config(main)
    monkeypatch.setattr("agent_core.config.get_config", lambda: cfg)

    persist_runtime_provider(
        "rt",
        {"base_url": "https://rt/v1", "api_key": "k", "model": "m-rt"},
        config_path=main,
    )
    assert (prov_dir / "_runtime.yaml").is_file()
    result = get_model_catalog().reload_from_disk(config=cfg, config_path=main)
    assert "rt" in result["added"]
    assert cfg.llm.providers["rt"].model == "m-rt"


# ---------------------------------------------------------------------------
# watcher fingerprint
# ---------------------------------------------------------------------------


def test_catalog_files_fingerprint_changes_on_add_and_edit(tmp_path):
    main, prov_dir = _make_disk_config(tmp_path)
    fp0 = catalog_files_fingerprint(main)
    assert any(str(main) == p for p, _ in fp0)

    _write_provider_file(prov_dir, "b", "mb")
    fp1 = catalog_files_fingerprint(main)
    assert fp1 != fp0
    assert any(p.endswith("b.yaml") for p, _ in fp1)

    import os
    import time

    p = prov_dir / "b.yaml"
    now = time.time() + 5
    os.utime(p, (now, now))
    fp2 = catalog_files_fingerprint(main)
    assert fp2 != fp1


def test_client_attach_is_weak():
    cfg = _make_config()
    with patch("agent_core.llm.providers.openai_compat.AsyncOpenAI"):
        client = LLMClient(config=cfg)
    catalog = get_model_catalog()
    assert catalog.attached_client_count == 1
    del client
    import gc

    gc.collect()
    assert catalog.attached_client_count == 0
