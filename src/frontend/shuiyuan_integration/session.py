"""
水源社区 Agent 会话入口。

设计：shuiyuan 前端 -> automation(connector) -> core(Agent) -> automation -> shuiyuan

1. automation 层：connector 轮询 @ 提及，准备上下文，调用 core
2. core 层：Agent 理解上下文，直接输出回复正文
3. automation 层：收到输出后调用 post_reply 发帖到水源

调用规则：必须同时满足「@ 主人」且「消息包含【玛奇朵】」才触发回复。
"""

from __future__ import annotations

from typing import Any, List, Optional

from agent_core.config import Config, get_config


def is_invocation_valid(
    raw_message: str,
    mentioned_usernames: List[str],
    *,
    config: Optional[Config] = None,
) -> tuple[bool, str]:
    """
    判断是否满足水源 Agent 调用规则：@ 主人 且 消息包含 invocation_trigger（默认【玛奇朵】）。

    Args:
        raw_message: 帖子原文
        mentioned_usernames: 被 @ 的用户名列表（来自 Discourse API）
        config: 配置对象，默认 get_config()

    Returns:
        (是否有效, 原因说明)
    """
    cfg = config or get_config()
    if not cfg.shuiyuan.enabled:
        return False, "水源未启用"

    owner = (cfg.shuiyuan.owner_username or "").strip()
    trigger = (cfg.shuiyuan.invocation_trigger or "【玛奇朵】").strip()

    if not trigger:
        return True, ""

    if trigger not in (raw_message or ""):
        return False, f"消息需包含 {trigger}"

    if owner:
        mentions = [u.strip().lower() for u in (mentioned_usernames or []) if u and isinstance(u, str)]
        if owner.lower() not in mentions:
            return False, f"需 @ {owner}"

    return True, ""


def is_invocation_valid_from_raw(raw_message: str, *, config: Optional[Config] = None) -> tuple[bool, str]:
    """
    从正文解析判断是否满足调用规则：@ 主人 且 消息包含 trigger。
    用于 topic 监控模式（无 user_actions/notifications，直接解析 raw）。

    Returns:
        (是否有效, 原因说明)
    """
    cfg = config or get_config()
    if not cfg.shuiyuan.enabled:
        return False, "水源未启用"

    owner = (cfg.shuiyuan.owner_username or "").strip()
    trigger = (cfg.shuiyuan.invocation_trigger or "【玛奇朵】").strip()

    raw = (raw_message or "").strip()
    if not trigger or trigger not in raw:
        return False, f"消息需包含 {trigger}"

    if owner:
        # raw 中 @ 格式：@username 或 /u/username（链接）
        owner_lower = owner.lower()
        raw_lower = raw.lower()
        at_owner = f"@{owner_lower}"
        if at_owner not in raw_lower and f"/u/{owner_lower}" not in raw_lower:
            return False, f"需 @ {owner}"

    return True, ""


from agent_core import ScheduleAgent
from agent_core.interfaces import AgentHooks, AgentRunInput
from agent_core.tools import ShuiyuanGetTopicTool, ShuiyuanRetortTool, ShuiyuanSearchTool

from system.automation import AutomationIPCClient, default_socket_path


async def _run_via_daemon(
    username: str,
    topic_id: int,
    ctx_user: str,
    reply_to_post_number: Optional[int],
    db: Any,
    client: Any,
) -> Optional[str]:
    """通过 daemon IPC 运行，使用 per-user 受限 Core。成功返回 str（可为空），daemon 不可用时返回 None。"""
    try:
        ipc = AutomationIPCClient(owner_id=username, source="shuiyuan", socket_path=default_socket_path())
        if not await ipc.ping():
            return None
    except Exception:
        return None

    await ipc.switch_session(f"shuiyuan:{username}", create_if_missing=True)
    result = await ipc.run_turn(AgentRunInput(text=ctx_user))
    reply_text = (result.output_text or "").strip()
    if reply_text:
        from .reply import post_reply
        success, msg = post_reply(
            username=username,
            topic_id=topic_id,
            raw=reply_text,
            reply_to_post_number=reply_to_post_number,
            db=db,
            client=client,
        )
        if not success:
            import logging
            logging.getLogger("shuiyuan_session").warning("发帖失败: %s", msg)
    return reply_text


async def run_shuiyuan_reply(
    username: str,
    topic_id: int,
    user_message: str,
    reply_to_post_number: Optional[int] = None,
    reply_to_post_id: Optional[int] = None,
    *,
    config: Optional[Config] = None,
    extra_tools: Optional[List[Any]] = None,
    thread_posts: Optional[List[dict]] = None,
) -> str:
    """
    水源社区 @ 触发时的回复流程。

    Args:
        username: 触发 @ 的用户名
        topic_id: 话题 ID
        user_message: 用户发来的消息内容
        reply_to_post_number: 要回复的楼层号（可选）
        reply_to_post_id: 触发帖的真实 post_id（可选；connector 传入后可注入供贴表情工具使用，避免 post_number≠post_id 导致 404）
        config: 配置对象，默认 get_config()
        extra_tools: 额外工具列表，可与 get_default_tools 合并
        thread_posts: 可选，该楼最近 N 条帖子（connector 已抓取时可传入，避免重复 API 请求导致 429）

    Returns:
        Agent 的回复文本（若调用了 shuiyuan_post_reply 则可能已发帖）
    """
    cfg = config or get_config()
    if not cfg.shuiyuan.enabled:
        return "水源社区未启用"

    # 调用规则：消息必须包含 invocation_trigger（默认【玛奇朵】）
    trigger = (cfg.shuiyuan.invocation_trigger or "【玛奇朵】").strip()
    if trigger and trigger not in (user_message or ""):
        return ""

    try:
        from frontend.shuiyuan_integration import (
            get_shuiyuan_client_from_config,
            record_user_message,
        )
        from frontend.shuiyuan_integration.reply import post_reply, get_shuiyuan_db_for_user
    except ImportError as e:
        return f"无法加载水源集成: {e}"

    client = get_shuiyuan_client_from_config(cfg)
    if not client:
        return "水源社区未配置 User-Api-Key，请设置 shuiyuan.user_api_key 或 SHUIYUAN_USER_API_KEY"

    db = get_shuiyuan_db_for_user(cfg, username)
    record_user_message(username, topic_id, user_message, db=db)

    # 组装初始上下文：该楼最近 N 条 + 用户聊天历史
    # 若 connector 已传入 thread_posts（topic 监控模式），直接复用，避免重复 get_topic_recent_posts 导致 429
    if thread_posts is not None:
        posts = thread_posts[: cfg.shuiyuan.memory.thread_posts_count]
    else:
        posts = client.get_topic_recent_posts(
            topic_id,
            limit=cfg.shuiyuan.memory.thread_posts_count,
        )
    thread_lines: List[str] = []
    for p in posts:
        pn = p.get("post_number", 0)
        pid = p.get("id")
        uname = p.get("username", "")
        raw = (p.get("raw") or p.get("cooked", ""))[:500]
        # 包含 post_id，供 shuiyuan_post_retort 使用（post_number≠post_id）
        pid_str = f" post_id={pid}" if pid is not None else ""
        thread_lines.append(f"[{pn}L]{pid_str} @{uname}: {raw}")

    chat_rows = db.get_chat(username)
    chat_lines: List[str] = []
    for row in chat_rows[-20:]:
        role = row.get("role", "user")
        content = row.get("content", "")[:300]
        chat_lines.append(f"[{role}]: {content}")

    ctx_user = ""
    if username:
        ctx_user += "## 当前对话用户\n\n"
        ctx_user += f"- 水源用户名：@{username}\n\n"
    if thread_lines:
        ctx_user += "## 该楼最近帖子\n\n" + "\n".join(thread_lines) + "\n\n"
    if chat_lines:
        if username:
            ctx_user += f"## 你与该用户（@{username}）的聊天历史（节选）\n\n"
        else:
            ctx_user += "## 你与该用户的聊天历史（节选）\n\n"
        ctx_user += "\n".join(chat_lines) + "\n\n"
    # 若已知触发楼层，补充该楼 post_id 供贴表情工具使用（post_number≠post_id，Retort API 需真实 post_id）
    trigger_post_id: Optional[int] = None
    if reply_to_post_id is not None:
        trigger_post_id = int(reply_to_post_id)
    elif reply_to_post_number is not None:
        for p in posts:
            pn = p.get("post_number")
            if pn is not None and int(pn) == int(reply_to_post_number):
                trigger_post_id = p.get("id")
                if trigger_post_id is not None:
                    trigger_post_id = int(trigger_post_id)
                break
    user_identity = f"（该楼作者用户名为 @{username}）" if username else ""
    ctx_user += (
        f"---\n用户 @了你，在当前话题 {topic_id}"
        + (
            f" 的第 {reply_to_post_number} 楼"
            + (f"（post_id={trigger_post_id}）" if trigger_post_id is not None else "")
            if reply_to_post_number is not None
            else ""
        )
        + user_identity
        + f"，说了：\n\n{user_message}\n\n请根据上文理解语境，直接输出你的回复正文（automation 层会自动发帖，不要调用发帖工具）。"
    )

    # 优先通过 daemon IPC（per-user 受限 Core），不可用时回退到本地 Agent
    via_daemon = await _run_via_daemon(
        username=username,
        topic_id=topic_id,
        ctx_user=ctx_user,
        reply_to_post_number=reply_to_post_number,
        db=db,
        client=client,
    )
    if via_daemon is not None:
        return via_daemon

    # 水源 session 日志（与主 Agent 分开，存到 logs/sessions/shuiyuan/）
    session_logger = None
    if getattr(cfg, "logging", None) and getattr(cfg.logging, "enable_session_log", False):
        from agent_core.utils.session_logger import SessionLogger
        from pathlib import Path
        log_dir = str(Path(getattr(cfg.logging, "session_log_dir", "./logs/sessions")).resolve() / "shuiyuan")
        session_logger = SessionLogger(
            log_dir=log_dir,
            enable_detailed_log=getattr(cfg.logging, "enable_detailed_log", False),
            max_system_prompt_log_len=getattr(cfg.logging, "max_system_prompt_log_len", 2000),
        )
        session_logger.on_session_start()

    # 水源 Agent 保留：联网搜索、URL 解析、水源搜索、获取话题（发帖由 automation 层负责）
    # web_search 和 extract_web_content 由 Agent 在 mcp.enabled 时自动注册
    max_posts = getattr(cfg.shuiyuan.memory, "tool_max_posts", 50) or 50
    tools: List[Any] = [
        ShuiyuanSearchTool(config=cfg, max_results=max_posts),
        ShuiyuanGetTopicTool(config=cfg, posts_limit=max_posts),
        ShuiyuanRetortTool(config=cfg),
    ]
    if extra_tools:
        tools.extend(extra_tools)

    agent_ref = None
    try:
        async with ScheduleAgent(
            config=cfg,
            tools=tools,
            max_iterations=cfg.agent.max_iterations,
            timezone=cfg.time.timezone,
            user_id=username,
            source="shuiyuan",
            session_logger=session_logger,
        ) as agent:
            agent_ref = agent
            output = await agent.process_input(ctx_user)
            reply_text = (output or "").strip()
            if reply_text:
                success, msg = post_reply(
                    username=username,
                    topic_id=topic_id,
                    raw=reply_text,
                    reply_to_post_number=reply_to_post_number,
                    db=db,
                    client=client,
                )
                if not success:
                    import logging
                    logging.getLogger("shuiyuan_session").warning("发帖失败: %s", msg)
            return reply_text
    finally:
        if session_logger is not None:
            turn_count = 1
            total_usage = None
            if agent_ref is not None:
                try:
                    turn_count = agent_ref.get_turn_count()
                    total_usage = agent_ref.get_token_usage()
                except Exception:
                    pass
            session_logger.on_session_end(turn_count=turn_count, total_usage=total_usage)
            session_logger.close()
