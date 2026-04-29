"""TokenUsage 与 DeepSeek prompt_cache_* 解析。"""

from types import SimpleNamespace

from agent_core.llm.response import TokenUsage


def test_token_usage_from_usage_with_cache_fields():
    u = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=10,
        total_tokens=110,
        prompt_cache_hit_tokens=80,
        prompt_cache_miss_tokens=20,
    )
    tu = TokenUsage.from_usage(u)
    assert tu.prompt_tokens == 100
    assert tu.completion_tokens == 10
    assert tu.total_tokens == 110
    assert tu.prompt_cache_hit_tokens == 80
    assert tu.prompt_cache_miss_tokens == 20


def test_token_usage_from_usage_missing_cache_defaults_zero():
    u = SimpleNamespace(
        prompt_tokens=17,
        completion_tokens=9,
        total_tokens=26,
    )
    tu = TokenUsage.from_usage(u)
    assert tu.prompt_cache_hit_tokens == 0
    assert tu.prompt_cache_miss_tokens == 0


def test_token_usage_from_response_nested_usage():
    resp = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=17,
            completion_tokens=9,
            total_tokens=26,
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=17,
        )
    )
    tu = TokenUsage.from_response(resp)
    assert tu.prompt_cache_miss_tokens == 17
