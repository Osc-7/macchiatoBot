"""
水源社区回复流程。

当 Agent 被 @ 调用时：
1. 读取 MEMORY.md（data/memory/long_term/shuiyuan）
2. 读取该用户最近 100 条聊天记录（shuiyuan.db）
3. 读取该楼最近 50 条帖子（Discourse API）
4. 组装 LLM 上下文
5. 限流检查后发帖并记录
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Optional

from .client import ShuiyuanClient, ShuiyuanClientPool
from .db import ShuiyuanDB


def load_shuiyuan_memory(memory_dir: str) -> str:
    """加载水源社区长期记忆 MEMORY.md。"""
    path = Path(memory_dir) / "MEMORY.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def build_reply_context(
    username: str,
    topic_id: int,
    reply_to_post_number: Optional[int],
    *,
    db: ShuiyuanDB,
    client: ShuiyuanClient,
    memory_dir: str,
    thread_posts_count: int = 50,
) -> List[dict[str, Any]]:
    """
    组装水源回复的 LLM 消息上下文。

    Returns:
        messages 列表，可直接传入 LLM chat 接口
    """
    messages: List[dict[str, Any]] = []

    # 1. 系统提示：长期记忆 + 水源规则
    memory = load_shuiyuan_memory(memory_dir)
    system_content = "## 水源社区 Agent 长期记忆\n\n" + (memory or "（暂无）") + "\n\n---\n\n"
    system_content += "你是水源社区中的 AI 助手，被 @ 调用后需根据该楼上下文和用户历史聊天记录，生成自然、友善的回复。"
    messages.append({"role": "system", "content": system_content})

    # 2. 该楼最近 N 条帖子（作为上文）
    posts = client.get_topic_recent_posts(topic_id, limit=thread_posts_count)
    thread_lines: List[str] = []
    for p in posts:
        pn = p.get("post_number", 0)
        uname = p.get("username", "")
        raw = p.get("raw", p.get("cooked", ""))[:500]
        thread_lines.append(f"[{pn}L] @{uname}: {raw}")
    if thread_lines:
        messages.append({
            "role": "user",
            "content": "## 该楼最近帖子\n\n" + "\n".join(thread_lines),
        })
        messages.append({
            "role": "assistant",
            "content": "已了解该楼上下文。",
        })

    # 3. 该用户与 Agent 的聊天历史
    chat_rows = db.get_chat(username)
    for row in chat_rows:
        role = row.get("role", "user")
        content = row.get("content", "")
        messages.append({"role": role, "content": content})

    return messages


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

    result, status_code, err_detail = client.create_post(
        raw=raw,
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
    db.append_chat(username, topic_id, "assistant", raw, post_id=post_id)

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


def get_shuiyuan_db_from_config(config: Any) -> ShuiyuanDB:
    """从 config 构建 ShuiyuanDB。"""
    cfg = config.shuiyuan
    return ShuiyuanDB(
        db_path=cfg.db_path,
        chat_limit_per_user=cfg.memory.chat_limit_per_user,
        replies_per_minute=cfg.rate_limit.replies_per_minute,
    )


def get_shuiyuan_client_from_config(config: Any) -> Optional[ShuiyuanClient]:
    """从 config 构建 ShuiyuanClient 或 ShuiyuanClientPool（支持多 Key 轮询与限流切换）。"""
    cfg = config.shuiyuan

    # 1. 优先使用配置中的 user_api_keys 列表
    keys: List[str] = []
    if getattr(cfg, "user_api_keys", None):
        keys = [k.strip() for k in cfg.user_api_keys if k and isinstance(k, str) and k.strip()]

    # 2. 回退到单个 user_api_key / 环境变量
    if not keys:
        single = cfg.user_api_key or os.environ.get("SHUIYUAN_USER_API_KEY")
        if single:
            keys = [single.strip()]

    if not keys:
        return None

    # 在数据库同目录下持久化 Key 状态，保证进程重启后冷却时间仍生效
    db_path = Path(cfg.db_path)
    state_path = db_path.with_name("user_api_keys_state.json")

    # 无论是单 Key 还是多 Key，都通过 ShuiyuanClientPool 管理，统一支持日级限流切换
    return ShuiyuanClientPool(
        user_api_keys=keys,
        site_url=cfg.site_url,
        state_path=state_path,
    )
