"""resolve_llm_provider_key：配置名、label、model ID 解析。"""

from __future__ import annotations

import pytest

from agent_core.config import CapabilitiesModel, Config, LLMConfig, ProviderEntry
from agent_core.llm.provider_resolve import resolve_llm_provider_key


def _cfg() -> Config:
    return Config(
        llm=LLMConfig(
            api_key="k",
            model="m",
            providers={
                "kimi_k25": ProviderEntry(
                    base_url="https://api.moonshot.cn/v1",
                    api_key="k",
                    model="kimi-k2.5",
                    label="Kimi K2.5",
                    capabilities=CapabilitiesModel(vision=True),
                ),
                "qwen_dashscope": ProviderEntry(
                    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    api_key="k",
                    model="qwen3.5-plus",
                    label="Qwen3.5 Plus",
                    capabilities=CapabilitiesModel(vision=True),
                ),
            },
            active="qwen_dashscope",
        ),
    )


def test_resolve_exact_key_and_case_insensitive():
    c = _cfg().llm
    assert resolve_llm_provider_key(c, "kimi_k25") == "kimi_k25"
    assert resolve_llm_provider_key(c, "KIMI_K25") == "kimi_k25"


def test_resolve_by_label_ignore_case_and_spaces():
    c = _cfg().llm
    assert resolve_llm_provider_key(c, "Kimi K2.5") == "kimi_k25"
    assert resolve_llm_provider_key(c, "  kimi   k2.5 ") == "kimi_k25"
    assert resolve_llm_provider_key(c, "Qwen3.5 Plus") == "qwen_dashscope"


def test_resolve_by_slug_hyphen_vs_space():
    c = _cfg().llm
    assert resolve_llm_provider_key(c, "Kimi-K2.5") == "kimi_k25"
    assert resolve_llm_provider_key(c, "qwen3.5-plus") == "qwen_dashscope"


def test_resolve_unknown_raises():
    c = _cfg().llm
    with pytest.raises(ValueError, match="未知 provider"):
        resolve_llm_provider_key(c, "nope")


def test_resolve_empty_query():
    c = _cfg().llm
    with pytest.raises(ValueError, match="不能为空"):
        resolve_llm_provider_key(c, "  ")
