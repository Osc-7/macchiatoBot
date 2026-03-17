"""
水源社区专用的上下文拼装工具（前端层）。

职责：
- 前端收集结构化上下文（最近帖子、聊天历史、楼层/作者信息等）
- 按既定水源业务文案模板拼成单轮 user prompt

注意：
- 该实现从历史的 `run_shuiyuan_reply` 中迁移而来，尽量保持文案和格式不变，
  避免影响线上用户体验。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _extract_topic_op(posts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """从帖子列表中抽取主楼（post_number==1），若不存在则取最早一楼。"""
    if not posts:
        return None
    op = None
    for p in posts:
        try:
            pn = int(p.get("post_number", 0) or 0)
        except Exception:
            pn = 0
        if pn == 1:
            op = p
            break
    if op is not None:
        return op
    # 回退：选 post_number 最小的一条
    try:
        return min(
            posts,
            key=lambda p: int(p.get("post_number", 1) or 1),
        )
    except Exception:
        return posts[0]


def _build_topic_op_section(topic_op: Dict[str, Any]) -> str:
    """生成「当前话题主楼」概览段落。"""
    if not topic_op:
        return ""
    title = (topic_op.get("topic_title") or "").strip()
    uname = (topic_op.get("username") or "").strip()
    raw = (topic_op.get("raw") or topic_op.get("cooked", "") or "")[:600]

    lines: List[str] = ["## 当前话题主楼\n"]
    if title:
        lines.append(f"- 标题：{title}\n")
    if uname:
        lines.append(f"- 楼主：@{uname}\n")
    if raw:
        lines.append("\n")
        lines.append(raw)
        lines.append("\n")
    lines.append("\n")
    return "".join(lines)


_BOT_REPLY_MARKER = "macchiato_SHUIYUAN_AUTO_REPLY"
_BOT_RAW_LIMIT = 100
_USER_RAW_LIMIT = 500


def _build_thread_section(posts: List[Dict[str, Any]]) -> str:
    """根据最近帖子列表生成「该楼最近帖子」段落。

    Bot 自身的回复（含 AUTO_REPLY_MARK）截短到 100 字符，
    避免历史回复中的过时模式被 LLM 当作 few-shot 示例。
    """
    lines: List[str] = []
    for p in posts:
        pn = p.get("post_number", 0)
        pid = p.get("id")
        uname = p.get("username", "")
        full_raw = p.get("raw") or p.get("cooked", "") or ""
        is_bot = _BOT_REPLY_MARKER in full_raw
        limit = _BOT_RAW_LIMIT if is_bot else _USER_RAW_LIMIT
        raw = full_raw[:limit]
        if len(full_raw) > limit:
            raw += "…"
        pid_str = f" post_id={pid}" if pid is not None else ""
        tag = " [bot]" if is_bot else ""
        lines.append(f"[{pn}L]{pid_str} @{uname}{tag}: {raw}")
    if not lines:
        return ""
    return "## 该楼最近帖子\n\n" + "\n".join(lines) + "\n\n"


def _build_trigger_footer(
    *,
    username: str,
    topic_id: int,
    reply_to_post_number: Optional[int],
    reply_to_post_id: Optional[int],
    user_message: str,
) -> str:
    """生成底部的触发楼描述 + 用户原话说明。"""
    trigger_post_id: Optional[int] = reply_to_post_id
    user_identity = f"（该层作者用户名为 @{username}）" if username else ""

    footer = (
        f"---\n用户 @了你，在当前话题 {topic_id}"
        + (
            f" 的第 {reply_to_post_number} 层"
            + (f"（post_id={trigger_post_id}）" if trigger_post_id is not None else "")
            if reply_to_post_number is not None
            else ""
        )
        + user_identity
        + "，说了：\n\n"
        f"{user_message}\n\n"
        "请根据上文理解语境，直接输出你的回复正文。"
    )
    return footer


def build_shuiyuan_prompt_from_context(
    *,
    context: Dict[str, Any],
    user_message: str,
) -> str:
    """
    从结构化水源上下文构建完整的 user 输入 prompt。

    context 预期结构::

        {
            "username": "Cattus",
            "topic_id": 346667,
            "reply_to_post_number": 3538,          # 可选
            "reply_to_post_id": 8383407,           # 可选
            "topic_op": {...},                     # 可选，话题主楼（如有则优先使用）
            "thread_posts": [ {...}, ... ],        # get_topic_recent_posts 或 connector 传入
        }
    """
    username = str(context.get("username") or "").strip()
    topic_id = int(context.get("topic_id") or 0)
    reply_to_post_number = context.get("reply_to_post_number")
    reply_to_post_id = context.get("reply_to_post_id")

    posts = context.get("thread_posts") or []
    if not isinstance(posts, list):
        posts = []

    parts: List[str] = []
    if username:
        parts.append("## 当前对话用户\n")
        parts.append(f"- 水源用户名：@{username}\n\n")

    # 话题主楼：优先使用 context.topic_op，其次从 posts 中推断
    topic_op = context.get("topic_op")
    if not isinstance(topic_op, dict) or not topic_op:
        topic_op = _extract_topic_op(posts)
    op_section = _build_topic_op_section(topic_op) if topic_op else ""
    if op_section:
        parts.append(op_section)

    thread_section = _build_thread_section(posts)
    if thread_section:
        parts.append(thread_section)

    footer = _build_trigger_footer(
        username=username,
        topic_id=topic_id,
        reply_to_post_number=reply_to_post_number,
        reply_to_post_id=reply_to_post_id,
        user_message=user_message,
    )
    parts.append(footer)

    return "".join(parts)
