"""
水源社区（上海交通大学 Discourse 论坛）集成模块。

提供 User-Api-Key 生成、Discourse API 客户端、聊天记录/限流、回复流程。
"""

from .client import ShuiyuanClient
from .db import ShuiyuanDB
from .reply import (
    get_shuiyuan_client_from_config,
    get_shuiyuan_db_for_user,
    post_reply,
    record_user_message,
)
from .session import is_invocation_valid, run_shuiyuan_reply

# connector 可单独运行: python -m shuiyuan_integration.connector
from .user_api_key import generate_user_api_key, UserApiKeyPayload, UserApiKeyRequestResult

__all__ = [
    "is_invocation_valid",
    "ShuiyuanClient",
    "ShuiyuanDB",
    "run_shuiyuan_reply",
    "get_shuiyuan_client_from_config",
    "get_shuiyuan_db_for_user",
    "post_reply",
    "record_user_message",
    "generate_user_api_key",
    "UserApiKeyPayload",
    "UserApiKeyRequestResult",
]
