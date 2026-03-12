"""
水源社区回复流程。

当 Agent 被 @ 调用时：
1. 读取该用户最近 N 条聊天记录（每用户独立 DB）
2. 读取该楼最近 N 条帖子（Discourse API）
3. 组装 LLM 上下文
4. 限流检查后发帖并记录
"""

from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Any, List, Optional

from .client import ShuiyuanClient, ShuiyuanClientPool
from .db import ShuiyuanDB, get_shuiyuan_db_path_for_user

# 固定标记，用于识别由本集成发出的自动回复，避免递归触发。
AUTO_REPLY_MARK = "macchiato_SHUIYUAN_AUTO_REPLY"


def _generate_random_string(length: int = 20) -> str:
    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(random.sample(chars, k=min(length, len(chars))))


def _attach_hidden_marker(raw: str) -> str:
    """
    在回复正文末尾附加一段不可见的 HTML 注释随机串和固定标记。

    - 若正文已包含 AUTO_REPLY_MARK，则不再重复附加（幂等）。
    - 形如:
        原文

        <!-- <random> -->
        <!-- macchiato_SHUIYUAN_AUTO_REPLY -->
    """
    text = raw or ""
    if AUTO_REPLY_MARK in text:
        return text
    random_suffix = _generate_random_string(20)
    marker_comment = f"<!-- {random_suffix} -->\n<!-- {AUTO_REPLY_MARK} -->"
    text = text.rstrip()
    return f"{text}\n\n{marker_comment}"


def post_reply(
    username: str,
    topic_id: int,
    raw: str,
    reply_to_post_number: Optional[int] = None,
    *,
    db: ShuiyuanDB,
    client: ShuiyuanClient,
) -> tuple[bool, str]:
    """
    检查限流后发帖，并保存到聊天记录。

    Returns:
        (success, message)
    """
    if not db.check_reply_allowed(username):
        return False, "限流：该用户在本分钟内回复次数已达上限，请稍后再试"

    raw_with_marker = _attach_hidden_marker(raw)

    result, status_code, err_detail = client.create_post(
        raw=raw_with_marker,
        topic_id=topic_id,
        reply_to_post_number=reply_to_post_number,
    )
    if not result:
        if status_code == 429:
            return False, "限流：水源 API 达到频率限制(429)，请稍后再试"
        if status_code == 403:
            return False, (
                "发帖失败：User-Api-Key 需含 write 权限。"
                "请运行 python -m shuiyuan_integration.user_api_key 并传入 scopes=['read','write'] 重新生成 Key。"
            )
        msg = f"发帖失败：HTTP {status_code}"
        if err_detail:
            msg += f" — {err_detail}"
        return False, msg

    db.record_reply(username)
    post_id = result.get("id")
    db.append_chat(username, topic_id, "assistant", raw_with_marker, post_id=post_id)

    return True, f"已回复，post_id={post_id}"


def record_user_message(
    username: str,
    topic_id: int,
    content: str,
    db: ShuiyuanDB,
    post_id: Optional[int] = None,
) -> None:
    """记录用户发来的消息（@ 触发时由 webhook 调用），以便后续 build_reply_context 能读到。"""
    db.append_chat(username, topic_id, "user", content, post_id=post_id)


def get_shuiyuan_db_for_user(config: Any, username: str) -> ShuiyuanDB:
    """从 config 构建该用户的 ShuiyuanDB（每用户独立 DB）。"""
    cfg = config.shuiyuan
    base_dir = getattr(cfg, "db_base_dir", None) or "./data/shuiyuan"
    db_path = get_shuiyuan_db_path_for_user(base_dir, username)
    return ShuiyuanDB(
        db_path=db_path,
        chat_limit_per_user=cfg.memory.chat_limit_per_user,
        replies_per_minute=cfg.rate_limit.replies_per_minute,
    )


def get_shuiyuan_client_from_config(
    config: Any,
) -> Optional[ShuiyuanClient | ShuiyuanClientPool]:
    """从 config 构建 ShuiyuanClient 或 ShuiyuanClientPool（支持多 Key 轮询与限流切换）。"""
    cfg = config.shuiyuan

    # 1. 优先使用配置中的 user_api_keys 列表
    keys: List[str] = []
    if getattr(cfg, "user_api_keys", None):
        keys = [
            k.strip()
            for k in cfg.user_api_keys
            if k and isinstance(k, str) and k.strip()
        ]

    # 2. 回退到单个 user_api_key / 环境变量
    if not keys:
        single = cfg.user_api_key or os.environ.get("SHUIYUAN_USER_API_KEY")
        if single:
            keys = [single.strip()]

    if not keys:
        return None

    # 在数据根目录下持久化 Key 状态，保证进程重启后冷却时间仍生效
    base_dir = Path(getattr(cfg, "db_base_dir", None) or "./data/shuiyuan")
    state_path = base_dir / "user_api_keys_state.json"

    # 无论是单 Key 还是多 Key，都通过 ShuiyuanClientPool 管理，统一支持日级限流切换
    return ShuiyuanClientPool(
        user_api_keys=keys,
        site_url=cfg.site_url,
        state_path=state_path,
    )
