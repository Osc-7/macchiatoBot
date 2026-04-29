"""计费模块测试"""

import pytest

from agent_core.utils.billing import (
    compute_cost,
    compute_cost_from_calls,
    get_model_prices,
)


class TestComputeCost:
    """compute_cost 测试（阶梯计费）"""

    def test_qwen35_plus_first_tier(self):
        """Qwen 3.5 Plus 首档：0<input≤128K，输入0.8/百万，输出4.8/百万"""
        # 10万输入 + 1万输出: 0.1*0.8 + 0.01*4.8 = 0.08 + 0.048 = 0.128
        cost = compute_cost(100_000, 10_000, "qwen3.5-plus")
        assert cost == pytest.approx(0.128, rel=1e-5)

    def test_qwen35_plus_second_tier(self):
        """Qwen 3.5 Plus 二档：128K<input≤256K"""
        # 15万输入触发二档: 2.0/百万输入, 12.0/百万输出
        cost = compute_cost(150_000, 5_000, "qwen3.5-plus")
        assert cost == pytest.approx(0.15 * 2.0 + 0.005 * 12.0, rel=1e-5)

    def test_qwen35_plus_model_name_variant(self):
        """支持 qwen-3.5-plus 写法"""
        cost1 = compute_cost(1000, 500, "qwen3.5-plus")
        cost2 = compute_cost(1000, 500, "qwen-3.5-plus")
        assert cost1 == cost2

    def test_unknown_model_returns_none(self):
        """未知模型返回 None"""
        assert compute_cost(1000, 500, "unknown-model") is None

    def test_zero_tokens(self):
        """零 token 返回 0"""
        cost = compute_cost(0, 0, "qwen3.5-plus")
        assert cost == 0.0

    def test_configured_cache_pricing(self):
        """配置价表支持 DeepSeek 类 cache hit/miss 分桶。"""
        pricing = {
            "deepseek-v4-flash": {
                "currency": "USD",
                "cny_per_currency_unit": 7.0,
                "input_cache_hit_per_million": 0.028,
                "input_cache_miss_per_million": 0.14,
                "output_per_million": 0.28,
            }
        }
        cost = compute_cost(
            1_000,
            100,
            "deepseek-v4-flash",
            pricing=pricing,
            prompt_cache_hit_tokens=800,
            prompt_cache_miss_tokens=200,
        )
        assert cost == pytest.approx(0.000549, rel=1e-5)


class TestComputeCostFromCalls:
    """compute_cost_from_calls 阶梯计费累加"""

    def test_multiple_calls_tiered(self):
        """多次调用各自按阶梯计费后累加"""
        calls = [(50_000, 1_000), (150_000, 2_000)]  # 首档 + 二档
        cost = compute_cost_from_calls(calls, "qwen3.5-plus")
        c1 = 0.05 * 0.8 + 0.001 * 4.8  # 0.04 + 0.0048 = 0.0448
        c2 = 0.15 * 2.0 + 0.002 * 12.0  # 0.3 + 0.024 = 0.324
        assert cost == pytest.approx(c1 + c2, rel=1e-5)

    def test_multiple_calls_configured_cache_pricing(self):
        pricing = {
            "deepseek-v4-flash": {
                "input_cache_hit_per_million": 0.028,
                "input_cache_miss_per_million": 0.14,
                "output_per_million": 0.28,
            }
        }
        calls = [
            (1_000, 100, 0, 1_000),
            (1_000, 100, 800, 200),
        ]
        cost = compute_cost_from_calls(calls, "deepseek-v4-flash", pricing=pricing)
        assert cost == pytest.approx(0.000246, rel=1e-5)


class TestGetModelPrices:
    """get_model_prices 测试"""

    def test_known_tiered_model(self):
        """阶梯模型返回 tiers"""
        p = get_model_prices("qwen3.5-plus")
        assert p is not None
        assert len(p) == 3  # 三档

    def test_unknown_model(self):
        """未知模型返回 None"""
        assert get_model_prices("unknown") is None

    def test_configured_model_price(self):
        pricing = {"my-model": {"input_per_million": 1.0, "output_per_million": 2.0}}
        assert get_model_prices("my_model", pricing=pricing) == pricing["my-model"]
