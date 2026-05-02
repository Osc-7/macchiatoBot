"""兼容层：每用户可读路径前缀持久化。"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from agent_core.agent.path_grants import (
    append_user_path_prefix,
    load_user_path_prefixes,
)

if TYPE_CHECKING:
    from agent_core.config import Config


def load_user_readable_prefixes(
    acl_base_dir: str,
    source: str,
    user_id: str,
    config: Optional["Config"] = None,
) -> List[str]:
    """读取已规范化绝对路径前缀列表；文件不存在返回空列表。"""
    return load_user_path_prefixes(
        acl_base_dir,
        source,
        user_id,
        access_mode="read",
    )


def append_user_readable_prefix(
    acl_base_dir: str,
    source: str,
    user_id: str,
    prefix_abs: str,
    config: Optional["Config"] = None,
) -> None:
    """幂等追加一条绝对路径前缀并写回 JSON。"""
    append_user_path_prefix(
        acl_base_dir,
        source,
        user_id,
        prefix_abs,
        access_mode="read",
        config=config,
    )
