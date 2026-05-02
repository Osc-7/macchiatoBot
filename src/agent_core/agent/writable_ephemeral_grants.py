"""兼容层：进程内临时可写路径前缀。"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from agent_core.agent.path_grants import (
    add_ephemeral_path_prefix,
    clear_ephemeral_path_grants_for_tests,
    list_ephemeral_path_prefixes,
)
if TYPE_CHECKING:
    from agent_core.config import Config


def add_ephemeral_writable_prefix(
    source: str,
    user_id: str,
    prefix_abs: str,
    *,
    config: Optional["Config"] = None,
) -> None:
    """为当前进程登记一条可写前缀（幂等追加）。"""
    add_ephemeral_path_prefix(
        source,
        user_id,
        prefix_abs,
        access_mode="write",
        config=config,
    )


def list_ephemeral_writable_prefixes(source: str, user_id: str) -> List[str]:
    return list_ephemeral_path_prefixes(source, user_id, access_mode="write")


def clear_ephemeral_writable_grants_for_tests() -> None:
    """测试用：清空进程内临时前缀。"""
    clear_ephemeral_path_grants_for_tests(access_mode="write")
