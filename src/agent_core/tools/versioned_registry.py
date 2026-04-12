"""
带版本号的工具注册表。

用于在 Agent Kernel 模式下支持：
- copy-on-write 更新
- snapshot 读取
- 基础关键字搜索
- 标签搜索
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseTool, ToolResult

# 极简停用词：削弱「的/什么/the」等对 IDF 的稀释（无 ML、无外部词表）
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "to",
        "of",
        "and",
        "or",
        "for",
        "in",
        "on",
        "at",
        "it",
        "do",
        "does",
        "did",
        "什么",
        "怎么",
        "如何",
        "哪些",
        "为什么",
        "的",
        "了",
        "和",
        "与",
        "或",
        "请",
        "帮我",
        "能否",
        "可以",
        "一下",
    }
)


def _tokenize_query(q: str) -> List[str]:
    q = (q or "").strip().lower()
    if not q:
        return []
    return [t for t in re.split(r"[\s,，。:：;；/\\|]+", q) if t]


def _cjk_ngram_bonus(q: str, corpus: str, cap: float = 2.4) -> float:
    """整句无空格时，用短子串在语料中做弱命中（中日文等）。"""
    if len(q) < 2:
        return 0.0
    bonus = 0.0
    for L in (2, 3, 4):
        if len(q) < L:
            continue
        step = max(1, (len(q) - L) // 14 + 1)
        for i in range(0, len(q) - L + 1, step):
            frag = q[i : i + L]
            if frag in corpus:
                bonus += 0.36
            if bonus >= cap:
                return cap
    return bonus


def _terms_for_weighted_match(tokens: List[str]) -> List[str]:
    """参与 IDF 加权的词项（去掉过短与停用词）。"""
    return [t for t in tokens if len(t) >= 2 and t not in _STOPWORDS]


def _idf_weight(df: int, n_docs: int) -> float:
    """经典 IDF 变体：log((N+1)/(df+1))+1，df 为含该词的候选工具数。"""
    return math.log((n_docs + 1.0) / (float(df) + 1.0)) + 1.0


def _tool_params_meta(definition: Any) -> List[Dict[str, Any]]:
    params_meta: List[Dict[str, Any]] = []
    for param in definition.parameters:
        params_meta.append(
            {
                "name": param.name,
                "type": param.type,
                "required": param.required,
                "description": param.description,
            }
        )
    return params_meta


@dataclass
class ToolSearchItem:
    """工具搜索返回项。"""

    name: str
    description: str
    parameters: List[Dict[str, Any]]
    tags: List[str]
    score: float
    weak_match: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """转换为可序列化字典。"""
        d: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "tags": self.tags,
            "score": round(self.score, 4),
        }
        if self.weak_match:
            d["weak_match"] = True
        return d


class VersionedToolRegistry:
    """
    带版本号和快照语义的工具注册表。

    说明：
    - 写操作采用 copy-on-write，确保读路径始终拿到稳定快照
    - 通过 version 变化感知工具集合更新
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._version = 0
        self._tools: Dict[str, BaseTool] = {}

    def list_tools(self) -> Tuple[int, Dict[str, BaseTool]]:
        """
        获取当前版本和工具快照。

        Returns:
            (version, tools_copy)
        """
        with self._lock:
            return self._version, self._tools.copy()

    def register(self, tool: BaseTool) -> None:
        """
        注册工具。

        Raises:
            ValueError: 工具名称已存在
        """
        with self._lock:
            if tool.name in self._tools:
                raise ValueError(f"工具 '{tool.name}' 已注册")
            next_tools = self._tools.copy()
            next_tools[tool.name] = tool
            self._tools = next_tools
            self._version += 1

    def update_tools(self, tools: List[BaseTool]) -> None:
        """
        批量更新工具（按 name 覆盖）。
        """
        with self._lock:
            next_tools = self._tools.copy()
            for tool in tools:
                next_tools[tool.name] = tool
            self._tools = next_tools
            self._version += 1

    def unregister(self, name: str) -> bool:
        """
        注销工具。
        """
        with self._lock:
            if name not in self._tools:
                return False
            next_tools = self._tools.copy()
            del next_tools[name]
            self._tools = next_tools
            self._version += 1
            return True

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._tools

    def get(self, name: str) -> Optional[BaseTool]:
        with self._lock:
            return self._tools.get(name)

    def list_names(self) -> List[str]:
        with self._lock:
            return list(self._tools.keys())

    def get_openai_tools(
        self, names: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        获取 OpenAI Function Calling 工具定义。
        """
        with self._lock:
            if names is None:
                tools = list(self._tools.values())
            else:
                tools = [self._tools[name] for name in names if name in self._tools]
        return [tool.to_openai_tool() for tool in tools]

    def get_all_definitions(self) -> List[Dict[str, Any]]:
        """
        兼容旧接口：返回全部工具定义。
        """
        return self.get_openai_tools()

    async def execute(self, tool_name: str, **kwargs) -> ToolResult:
        """
        执行工具。工具内部未捕获的异常将被包装为 ToolResult(success=False)，
        保证调用方始终收到结构化结果，不因工具 bug 导致整个 kernel 循环崩溃。
        """
        tool = self.get(tool_name)
        if tool is None:
            return ToolResult(
                success=False,
                error="TOOL_NOT_FOUND",
                message=f"工具 '{tool_name}' 不存在",
            )
        try:
            return await tool.execute(**kwargs)
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).exception(
                "Tool '%s' raised an unhandled exception", tool_name
            )
            return ToolResult(
                success=False,
                error="TOOL_EXCEPTION",
                message=f"工具 '{tool_name}' 执行时发生未捕获异常: {exc}",
            )

    def search(
        self,
        query: str,
        limit: int = 8,
        exclude_names: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        name_prefix: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        按关键字和/或标签搜索工具。

        支持（均为确定性、无机器学习排序）：
        - 关键词：匹配 name / description / 参数 / usage_notes / **工具自身 tags 文本**
        - **IDF 加权**：分词在越少工具中出现的，命中加分越高（经典 log(N/df)+1）
        - **停用词表**：削弱「的/the/什么」等对匹配的稀释
        - 名称子串：整 query 或分词命中 **工具名** 时加分（同样可带 IDF）
        - 中日文：整句无空格时辅以短 n-gram 在语料中弱命中
        - 标签筛选：与现有一致（传入 tags 时至少命中其一）
        - 名称前缀：name_prefix 过滤
        - **零强命中时**：用名称/描述与 query 的相似度弱排序；仍无则返回候选集字典序弱推荐（标记 weak_match）

        Args:
            query: 搜索关键词（可为空）
            limit: 返回数量上限
            exclude_names: 要排除的工具名称列表
            tags: 要匹配的标签列表（可为空）
            name_prefix: 工具名前缀过滤（可为空）

        Returns:
            搜索结果列表，按分数降序排列
        """
        with self._lock:
            tools = self._tools.copy()

        exclude = set(exclude_names or [])
        q = (query or "").strip().lower()
        tokens = _tokenize_query(q)
        tag_filter = {
            str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()
        }
        pref = (name_prefix or "").strip()

        candidates: List[Tuple[str, BaseTool, Any, set]] = []
        for name, tool in tools.items():
            if name in exclude:
                continue
            if pref and not name.startswith(pref):
                continue
            definition = tool.get_definition()
            def_tags = {
                str(tag).strip().lower()
                for tag in (definition.tags or [])
                if str(tag).strip()
            }
            if tag_filter and not tag_filter.intersection(def_tags):
                continue
            candidates.append((name, tool, definition, def_tags))

        prepared: List[Tuple[str, Any, Any, set, str, List[Dict[str, Any]]]] = []
        for name, tool, definition, def_tags in candidates:
            text_parts: List[str] = [name, definition.description or ""]
            for note in definition.usage_notes or []:
                text_parts.append(str(note))
            for param in definition.parameters:
                text_parts.extend([param.name, param.description])
            if def_tags:
                text_parts.append(" ".join(sorted(def_tags)))
            corpus = " ".join(text_parts).lower()
            params_meta = _tool_params_meta(definition)
            prepared.append((name, tool, definition, def_tags, corpus, params_meta))

        n_docs = max(1, len(prepared))
        term_set = _terms_for_weighted_match(tokens)
        token_df: Dict[str, int] = {}
        for _name, _tool, _definition, _def_tags, corpus, _pm in prepared:
            for term in term_set:
                if term in corpus:
                    token_df[term] = token_df.get(term, 0) + 1

        items: List[ToolSearchItem] = []

        for name, tool, definition, def_tags, corpus, params_meta in prepared:
            score = 0.0
            nm = name.lower()

            if q:
                if q in corpus:
                    score += 2.0
                if term_set:
                    for term in term_set:
                        if term in corpus:
                            score += _idf_weight(token_df[term], n_docs) * 1.08
                else:
                    for token in tokens:
                        if len(token) >= 2 and token in corpus:
                            score += 1.0
                if q in nm:
                    score += 1.65
                if term_set:
                    for term in term_set:
                        if len(term) >= 2 and term in nm:
                            score += 0.88 * _idf_weight(token_df.get(term, 0), n_docs)
                else:
                    for token in tokens:
                        if len(token) >= 2 and token in nm:
                            score += 0.9
                # 仅中日文查询启用 n-gram，避免英文长串的随机双字母误命中工具名/描述
                if len(tokens) <= 2 and re.search(r"[\u4e00-\u9fff]", q):
                    score += _cjk_ngram_bonus(q, corpus)
            else:
                score = 1.0

            if tag_filter:
                matched_tags = tag_filter.intersection(def_tags)
                score += len(matched_tags) * 0.55

            if score <= 0:
                continue

            items.append(
                ToolSearchItem(
                    name=name,
                    description=definition.description,
                    parameters=params_meta,
                    tags=definition.tags,
                    score=score,
                    weak_match=False,
                )
            )

        items.sort(key=lambda x: (-x.score, x.name))

        if not items and q and candidates:
            weak: List[ToolSearchItem] = []
            for name, tool, definition, def_tags in candidates:
                desc = (definition.description or "")[:400].lower()
                r = max(
                    SequenceMatcher(None, q, name.lower()).ratio(),
                    SequenceMatcher(None, q, desc).ratio() * 0.92,
                )
                if r < 0.24:
                    continue
                weak.append(
                    ToolSearchItem(
                        name=name,
                        description=definition.description,
                        parameters=_tool_params_meta(definition),
                        tags=definition.tags,
                        score=max(0.25, r * 2.2),
                        weak_match=True,
                    )
                )
            weak.sort(key=lambda x: (-x.score, x.name))
            items = weak[:limit] if limit > 0 else weak

        if not items and candidates and limit > 0:
            pool = sorted(candidates, key=lambda c: c[0])[:limit]
            items = [
                ToolSearchItem(
                    name=n,
                    description=defn.description,
                    parameters=_tool_params_meta(defn),
                    tags=defn.tags,
                    score=0.02,
                    weak_match=True,
                )
                for n, _t, defn, _dt in pool
            ]

        if limit > 0:
            items = items[:limit]
        return [item.to_dict() for item in items]

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return self.has(name)
