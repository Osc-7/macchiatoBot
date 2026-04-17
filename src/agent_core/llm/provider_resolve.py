"""
将用户输入解析为 config.llm.providers 的注册名（key）。

支持：配置名（大小写不敏感）、YAML ``label``、以及 ``model``（API 模型 ID）的宽松匹配，
便于 ``/model Kimi K2.5``、``/model kimi-k2.5`` 等与列表展示一致。
"""

from __future__ import annotations

import re
from typing import Any, List


def _slug(s: str) -> str:
    """仅保留小写字母与数字，用于忽略空格、连字符、斜杠等差异。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _norm_ws(s: str) -> str:
    """小写 + 折叠空白。"""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def resolve_llm_provider_key(llm_config: Any, query: str) -> str:
    """
    将 ``query`` 解析为 ``providers`` 的 key。

    匹配顺序：精确 key → 忽略大小写的 key → 与 label 全字匹配（忽略大小写）
    → 与 label 规范化空白后匹配 → slug 与 key / label / model 匹配
    → 若仅有一个 provider 的 label slug 以 query slug 为前缀且 query 长度≥4，则命中（唯一时）。
    """
    raw = (query or "").strip()
    if not raw:
        raise ValueError("provider 查询不能为空")

    prov_map = getattr(llm_config, "providers", None) or {}
    if not prov_map:
        raise ValueError("未配置 llm.providers")

    def _fail() -> None:
        hints: List[str] = []
        for k, ent in prov_map.items():
            lab = getattr(ent, "label", None)
            mid = getattr(ent, "model", None)
            if lab:
                hints.append(f"{k}（label: {lab}）")
            elif mid:
                hints.append(f"{k}（model: {mid}）")
            else:
                hints.append(k)
        raise ValueError(
            f"未知 provider: {raw!r}。可尝试配置名或备注 label，例如：{'; '.join(hints[:8])}"
        )

    # 1) 精确 key
    if raw in prov_map:
        return raw

    # 2) 忽略大小写 key
    rl = raw.lower()
    for k in prov_map:
        if k.lower() == rl:
            return k

    # 3) label 全字（忽略大小写）
    for k, ent in prov_map.items():
        lab = getattr(ent, "label", None)
        if lab is not None and str(lab).strip().lower() == raw.lower():
            return k

    # 4) label 规范化空白后相等
    nraw = _norm_ws(raw)
    for k, ent in prov_map.items():
        lab = getattr(ent, "label", None)
        if lab is not None and _norm_ws(str(lab)) == nraw:
            return k

    rq = _slug(raw)
    if rq:
        # 5) slug：key / label / model
        for k, ent in prov_map.items():
            if _slug(k) == rq:
                return k
        for k, ent in prov_map.items():
            lab = getattr(ent, "label", None)
            if lab is not None and _slug(str(lab)) == rq:
                return k
            mid = getattr(ent, "model", None)
            if mid is not None and _slug(str(mid)) == rq:
                return k

        # 6) 唯一前缀（如仅输入 "kimi" 且只有一个 Kimi label）
        if len(rq) >= 3:
            matches = []
            for k, ent in prov_map.items():
                lab = getattr(ent, "label", None)
                if lab is None:
                    continue
                ls = _slug(str(lab))
                if ls == rq or (ls.startswith(rq) and len(rq) >= 4):
                    matches.append(k)
            if len(matches) == 1:
                return matches[0]

    _fail()
