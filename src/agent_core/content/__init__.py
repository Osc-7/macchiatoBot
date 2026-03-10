"""内容解析器：将前端来源的图片/文件/视频等统一解析为 LLM 可用的 content item。"""

from .models import ContentReference
from .resolver import ContentResolver, get_content_resolver, resolve_content_refs, register_resolver

__all__ = [
    "ContentReference",
    "ContentResolver",
    "get_content_resolver",
    "resolve_content_refs",
    "register_resolver",
]
