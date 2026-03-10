"""内容解析器：根据 ContentReference 解析为 LLM-ready content items。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .models import ContentReference

logger = logging.getLogger(__name__)

# 全局 Resolver 注册表：source -> resolver 实例
_resolvers: Dict[str, "ContentResolver"] = {}
_builtins_loaded: bool = False


class ContentResolver:
    """内容解析器基类。"""

    source: str = "unknown"

    async def resolve(self, ref: ContentReference) -> Optional[Dict[str, Any]]:
        """
        将单个 ContentReference 解析为 LLM-ready content item。

        Returns:
            image_url / video_url 格式的 content item，或 None（解析失败）
        """
        raise NotImplementedError

    async def resolve_batch(
        self,
        refs: List[ContentReference],
    ) -> List[Dict[str, Any]]:
        """批量解析，返回成功的 content items（跳过失败的）。"""
        result: List[Dict[str, Any]] = []
        for ref in refs:
            if ref.source != self.source:
                continue
            try:
                item = await self.resolve(ref)
                if item:
                    result.append(item)
            except Exception as exc:
                logger.warning("content resolver %s failed for ref %s: %s", self.source, ref.key, exc)
        return result


def _ensure_resolvers() -> None:
    """延迟导入并注册所有内置 Resolver。"""
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True
    try:
        from .resolvers.local import LocalContentResolver
        _resolvers.setdefault("local", LocalContentResolver())
    except ImportError:
        pass


def register_resolver(resolver: ContentResolver) -> None:
    """注册 Resolver。"""
    _resolvers[resolver.source] = resolver


def get_content_resolver(source: str) -> Optional[ContentResolver]:
    """获取指定 source 的 Resolver。"""
    _ensure_resolvers()
    return _resolvers.get(source)


async def resolve_content_refs(
    refs: List[ContentReference],
) -> List[Dict[str, Any]]:
    """
    根据 ContentReference 列表解析出 LLM-ready content items。

    按 source 分发到对应 Resolver，合并结果。
    """
    _ensure_resolvers()
    result: List[Dict[str, Any]] = []
    by_source: Dict[str, List[ContentReference]] = {}
    for ref in refs:
        if not ref or not ref.key:
            continue
        by_source.setdefault(ref.source, []).append(ref)

    for source, ref_list in by_source.items():
        resolver = _resolvers.get(source)
        if not resolver:
            logger.warning("no content resolver for source=%s, skip %d refs", source, len(ref_list))
            continue
        items = await resolver.resolve_batch(ref_list)
        result.extend(items)

    return result
