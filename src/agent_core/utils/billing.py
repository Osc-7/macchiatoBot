"""
LLM 计费工具

根据 token 用量和模型单价计算费用（人民币元）。
优先使用 provider 配置里的 ``pricing``；未配置时保留少量内置旧价表作为回退。
支持阶梯计费（按单次请求的输入 token 数分档）与 DeepSeek 类 cache hit/miss 分桶。
参考：https://help.aliyun.com/zh/model-studio/model-pricing
"""

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# 阶梯定义：(输入token上限, 输入单价, 输出单价) 元/百万token，中国内地
# 按单次请求的输入 Token 数分档
_TIERED_PRICES: Dict[str, List[Tuple[int, float, float]]] = {
    "qwen3.5-plus": [
        (128_000, 0.8, 4.8),  # 0 < input ≤ 128K
        (256_000, 2.0, 12.0),  # 128K < input ≤ 256K
        (1_000_000, 4.0, 24.0),  # 256K < input ≤ 1M
    ],
    "qwen-3.5-plus": [
        (128_000, 0.8, 4.8),
        (256_000, 2.0, 12.0),
        (1_000_000, 4.0, 24.0),
    ],
    "qwen3.5-plus-2026-02-15": [
        (128_000, 0.8, 4.8),
        (256_000, 2.0, 12.0),
        (1_000_000, 4.0, 24.0),
    ],
}

# 无阶梯模型：简单 (input_per_million, output_per_million)
_FLAT_PRICES: Dict[str, Tuple[float, float]] = {
    "qwen-turbo": (0.3, 0.6),
    "qwen-plus": (0.8, 2.0),  # qwen-plus 也有阶梯，此处用首档近似
    "qwen-max": (2.4, 9.6),
}


def _get_tier_prices(
    prompt_tokens: int, tiers: List[Tuple[int, float, float]]
) -> Tuple[float, float]:
    """根据单次请求的输入 token 数获取对应阶梯单价"""
    for limit, inp, out in tiers:
        if prompt_tokens <= limit:
            return (inp, out)
    # 超过最大阶梯，用最后一档
    return (tiers[-1][1], tiers[-1][2])


def _compute_cost_per_call(
    prompt_tokens: int, completion_tokens: int, model: str
) -> Optional[float]:
    """
    单次调用的阶梯计费。

    Args:
        prompt_tokens: 该次调用的输入 token 数
        completion_tokens: 该次调用的输出 token 数
        model: 模型名称

    Returns:
        该次调用的费用（元），未知模型返回 None
    """
    key = model if model in _TIERED_PRICES else model.lower().replace("_", "-")
    tiers = _TIERED_PRICES.get(key)
    if tiers:
        inp_p, out_p = _get_tier_prices(prompt_tokens, tiers)
        cost = (prompt_tokens / 1_000_000 * inp_p) + (
            completion_tokens / 1_000_000 * out_p
        )
        return round(cost, 6)

    # 无阶梯模型
    prices = _FLAT_PRICES.get(key) or _FLAT_PRICES.get(model.lower().replace("_", "-"))
    if prices is None:
        return None
    inp_p, out_p = prices
    cost = (prompt_tokens / 1_000_000 * inp_p) + (completion_tokens / 1_000_000 * out_p)
    return round(cost, 6)


def _model_key(model: str) -> str:
    return (model or "").lower().replace("_", "-")


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _resolve_pricing(model: str, pricing: Optional[Mapping[str, Any]]) -> Any:
    if not pricing:
        return None
    if model in pricing:
        return pricing[model]
    key = _model_key(model)
    for k, v in pricing.items():
        if _model_key(str(k)) == key:
            return v
    return None


def _unpack_call(call: Any) -> Tuple[int, int, int, int]:
    """返回 (prompt, completion, cache_hit, cache_miss)。兼容旧二元组。"""
    if isinstance(call, Mapping):
        return (
            int(call.get("prompt_tokens", 0) or 0),
            int(call.get("completion_tokens", 0) or 0),
            int(call.get("prompt_cache_hit_tokens", 0) or 0),
            int(call.get("prompt_cache_miss_tokens", 0) or 0),
        )
    if isinstance(call, (tuple, list)):
        pt = int(call[0] if len(call) > 0 else 0)
        ct = int(call[1] if len(call) > 1 else 0)
        hit = int(call[2] if len(call) > 2 else 0)
        miss = int(call[3] if len(call) > 3 else 0)
        return pt, ct, hit, miss
    return 0, 0, 0, 0


def _compute_configured_cost_per_call(
    call: Any,
    model: str,
    pricing: Optional[Mapping[str, Any]],
) -> Optional[float]:
    cfg = _resolve_pricing(model, pricing)
    if cfg is None:
        return None

    prompt_tokens, completion_tokens, hit_tokens, miss_tokens = _unpack_call(call)
    exchange = float(_get_attr(cfg, "cny_per_currency_unit", 1.0) or 1.0)
    output_p = _get_attr(cfg, "output_per_million")

    tiers = list(_get_attr(cfg, "tiers", []) or [])
    if tiers:
        tier_tuples: List[Tuple[int, float, float]] = []
        for t in tiers:
            tier_tuples.append(
                (
                    int(_get_attr(t, "input_token_limit", 0) or 0),
                    float(_get_attr(t, "input_per_million", 0.0) or 0.0),
                    float(_get_attr(t, "output_per_million", 0.0) or 0.0),
                )
            )
        tier_tuples.sort(key=lambda x: x[0])
        inp_p, out_p = _get_tier_prices(prompt_tokens, tier_tuples)
        return round(
            (
                prompt_tokens / 1_000_000 * inp_p
                + completion_tokens / 1_000_000 * out_p
            )
            * exchange,
            6,
        )

    if output_p is None:
        return None
    out_p = float(output_p)

    hit_p = _get_attr(cfg, "input_cache_hit_per_million")
    miss_p = _get_attr(cfg, "input_cache_miss_per_million")
    input_p = _get_attr(cfg, "input_per_million")
    if hit_p is not None and miss_p is not None:
        # 有 cache 分桶价格时：已知 hit/miss 分桶按分桶计；未上报的剩余输入按 miss 处理。
        remainder = max(prompt_tokens - hit_tokens - miss_tokens, 0)
        input_cost = (
            hit_tokens / 1_000_000 * float(hit_p)
            + (miss_tokens + remainder) / 1_000_000 * float(miss_p)
        )
    elif input_p is not None:
        input_cost = prompt_tokens / 1_000_000 * float(input_p)
    else:
        return None

    output_cost = completion_tokens / 1_000_000 * out_p
    return round((input_cost + output_cost) * exchange, 6)


def compute_cost(
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
    pricing: Optional[Mapping[str, Any]] = None,
    prompt_cache_hit_tokens: int = 0,
    prompt_cache_miss_tokens: int = 0,
) -> Optional[float]:
    """
    计算单次调用的费用（元），支持阶梯计费。

    Args:
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数
        model: 模型名称（如 qwen3.5-plus）

    Returns:
        费用（人民币元），未知模型返回 None
    """
    configured = _compute_configured_cost_per_call(
        (
            prompt_tokens,
            completion_tokens,
            prompt_cache_hit_tokens,
            prompt_cache_miss_tokens,
        ),
        model,
        pricing,
    )
    if configured is not None:
        return configured
    return _compute_cost_per_call(prompt_tokens, completion_tokens, model)


def compute_cost_from_calls(
    calls: Sequence[Any],
    model: str,
    pricing: Optional[Mapping[str, Any]] = None,
) -> Optional[float]:
    """
    根据多次调用的 (prompt_tokens, completion_tokens) 列表，按阶梯计费累加总费用。

    Args:
        calls: [(prompt_tokens, completion_tokens), ...] 或
               [(prompt_tokens, completion_tokens, cache_hit, cache_miss), ...]
        model: 模型名称

    Returns:
        总费用（元），未知模型返回 None
    """
    total = 0.0
    for call in calls:
        c = _compute_configured_cost_per_call(call, model, pricing)
        if c is None:
            pt, ct, _, _ = _unpack_call(call)
            c = _compute_cost_per_call(pt, ct, model)
        if c is None:
            return None
        total += c
    return round(total, 6)


def get_model_prices(
    model: str, pricing: Optional[Mapping[str, Any]] = None
) -> Optional[object]:
    """
    获取模型定价信息（阶梯或单价）。

    Returns:
        阶梯模型返回 tiers 列表，平价为 (input, output)，未知返回 None
    """
    configured = _resolve_pricing(model, pricing)
    if configured is not None:
        return configured
    key = model if model in _TIERED_PRICES else model.lower().replace("_", "-")
    if key in _TIERED_PRICES:
        return _TIERED_PRICES[key]
    if key in _FLAT_PRICES:
        return _FLAT_PRICES[key]
    return None
