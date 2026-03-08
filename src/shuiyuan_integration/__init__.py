"""
水源社区（上海交通大学 Discourse 论坛）集成模块。

提供 User-Api-Key 生成与 Discourse API 客户端，供 Agent 访问水源社区。
"""

from .client import ShuiyuanClient
from .user_api_key import generate_user_api_key, UserApiKeyPayload, UserApiKeyRequestResult

__all__ = [
    "ShuiyuanClient",
    "generate_user_api_key",
    "UserApiKeyPayload",
    "UserApiKeyRequestResult",
]
