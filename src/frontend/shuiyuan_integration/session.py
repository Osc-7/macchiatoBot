"""
水源社区 Agent 会话入口。

设计：shuiyuan 前端 -> automation(connector) -> core(Agent) -> automation -> shuiyuan

1. automation 层：connector 轮询 @ 提及，准备上下文，调用 core
2. core 层：Agent 理解上下文，直接输出回复正文；可调用 attach_image_to_reply 登记附件
3. automation 层：收到输出后，先将附件上传到水源（upload_file），再将图片 Markdown 拼入
   正文并调用 post_reply 发帖到水源

调用规则：必须同时满足「@ 主人」且「消息包含【玛奇朵】」才触发回复。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_core.config import Config, get_config
from frontend.shuiyuan_integration.reply import AUTO_REPLY_MARK

from agent_core import AgentCore
from agent_core.content import ContentReference
from agent_core.interfaces import AgentRunInput
from agent_core.tools import (
    AttachImageToReplyTool,
    ShuiyuanGetTopicTool,
    ShuiyuanRetortTool,
    ShuiyuanSearchTool,
)

from system.automation import AutomationIPCClient, default_socket_path

logger = logging.getLogger("shuiyuan_session")

_MIME_BY_EXT: Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


async def _upload_and_embed_attachments(
    attachments: List[Dict[str, Any]],
    *,
    client: Any,
) -> str:
    """
    将 Agent 登记的附件逐一上传到水源，返回要追加到回复末尾的 Markdown 图片串。

    若上传失败则跳过该附件并记录 warning，不影响文字回复。
    """
    if not attachments:
        return ""

    md_parts: List[str] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        if att.get("type") != "image":
            continue

        img_path = att.get("path")
        img_url = att.get("url")

        try:
            if img_path:
                p = Path(img_path)
                if not p.exists():
                    logger.warning("attachment file not found: %s", img_path)
                    continue
                file_bytes = p.read_bytes()
                filename = p.name
                mime = _MIME_BY_EXT.get(p.suffix.lower(), "image/png")
            elif img_url:
                import requests as _req

                resp = await asyncio.to_thread(_req.get, img_url, timeout=15.0)
                resp.raise_for_status()
                file_bytes = resp.content
                ct = resp.headers.get("Content-Type", "image/png").split(";", 1)[0].strip()
                mime = ct if ct and ct != "application/octet-stream" else "image/png"
                url_path = img_url.rsplit("?", 1)[0]
                filename = url_path.rsplit("/", 1)[-1] or "image.png"
            else:
                continue

            upload_result = await asyncio.to_thread(
                client.upload_file,
                file_bytes,
                filename,
                mime_type=mime,
            )
            if upload_result and upload_result.get("short_url"):
                short_url = upload_result["short_url"]
                w = upload_result.get("width", "")
                h = upload_result.get("height", "")
                size = f"|{w}x{h}" if w and h else ""
                md_parts.append(f"![image{size}]({short_url})")
            else:
                logger.warning(
                    "upload_file returned no short_url for attachment: %s", att
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to upload attachment %s: %s", att, exc)

    return ("\n\n" + "\n".join(md_parts)) if md_parts else ""


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

    # 若正文中已包含自动回复标记，说明是本 Agent 之前的回复或被引用，直接跳过以避免递归调用。
    if AUTO_REPLY_MARK in (raw_message or ""):
        return False, "检测到自动回复标记，跳过以避免递归回复"

    if not trigger:
        return True, ""

    if trigger not in (raw_message or ""):
        return False, f"消息需包含 {trigger}"

    if owner:
        mentions = [
            u.strip().lower()
            for u in (mentioned_usernames or [])
            if u and isinstance(u, str)
        ]
        if owner.lower() not in mentions:
            return False, f"需 @ {owner}"

    return True, ""


def is_invocation_valid_from_raw(
    raw_message: str, *, config: Optional[Config] = None
) -> tuple[bool, str]:
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
    # 若正文中已包含自动回复标记，说明是本 Agent 之前的回复或被引用，直接跳过以避免递归调用。
    if AUTO_REPLY_MARK in raw:
        return False, "检测到自动回复标记，跳过以避免递归回复"
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


async def _run_via_daemon(
    username: str,
    topic_id: int,
    ctx_user: str,
    reply_to_post_number: Optional[int],
    db: Any,
    client: Any,
    *,
    content_refs: Optional[List[ContentReference]] = None,
) -> Optional[str]:
    """通过 daemon IPC 运行，使用 per-user 受限 Core。成功返回 str（可为空），daemon 不可用时返回 None。"""
    try:
        ipc = AutomationIPCClient(
            owner_id=username, source="shuiyuan", socket_path=default_socket_path()
        )
        if not await ipc.ping():
            return None
    except Exception:
        return None

    await ipc.switch_session(f"shuiyuan:{username}", create_if_missing=True)

    # daemon 进程不一定注册了 ShuiyuanContentResolver，因此在 connector
    # 进程侧就地 resolve content_refs → content_items（base64 data URL），
    # 通过 metadata["content_items"] 直接传给 CoreSessionAdapter。
    metadata: Dict[str, Any] = {}
    if content_refs:
        try:
            from agent_core.content import resolve_content_refs

            logger.info(
                "shuiyuan: resolving %d content_refs before IPC for user=%s topic=%s",
                len(content_refs),
                username,
                topic_id,
            )
            resolved = await resolve_content_refs(content_refs)
            if resolved:
                # 只记录元信息，避免在日志里打印整段 base64
                logger.info(
                    "shuiyuan: resolved %d content_items before IPC (first_types=%s)",
                    len(resolved),
                    [str(i.get("type")) for i in resolved[:3]],
                )
                metadata["content_items"] = resolved
            else:
                logger.warning(
                    "shuiyuan: resolve_content_refs returned empty for user=%s topic=%s",
                    username,
                    topic_id,
                )
        except Exception as exc:
            logger.warning("resolve content_refs before IPC failed: %s", exc)

    result = await ipc.run_turn(AgentRunInput(text=ctx_user, metadata=metadata))
    reply_text = (result.output_text or "").strip()

    # 处理 attach_image_to_reply 登记的附件：上传并拼接 Markdown
    attach_md = await _upload_and_embed_attachments(
        list(getattr(result, "attachments", None) or []),
        client=client,
    )
    final_text = reply_text + attach_md

    if final_text:
        from .reply import post_reply

        success, msg = post_reply(
            username=username,
            topic_id=topic_id,
            raw=final_text,
            reply_to_post_number=reply_to_post_number,
            db=db,
            client=client,
        )
        if not success:
            logger.warning("发帖失败: %s", msg)
    return final_text


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
        from frontend.shuiyuan_integration.reply import (
            post_reply,
            get_shuiyuan_db_for_user,
        )
    except ImportError as e:
        return f"无法加载水源集成: {e}"

    client = get_shuiyuan_client_from_config(cfg)
    if not client:
        return "水源社区未配置 User-Api-Key，请设置 shuiyuan.user_api_key 或 SHUIYUAN_USER_API_KEY"

    db = get_shuiyuan_db_for_user(cfg, username)
    record_user_message(username, topic_id, user_message, db=db)

    # 解析用户帖子中的图片 upload:// 引用，转为 LLM content_refs（供模型看图）
    from .content_parser import parse_shuiyuan_raw_images

    site_url = getattr(cfg.shuiyuan, "site_url", "") or "https://shuiyuan.sjtu.edu.cn"
    content_refs, cleaned_message = parse_shuiyuan_raw_images(
        user_message, site_url=site_url
    )
    if content_refs:
        logger.info(
            "shuiyuan: parsed %d image refs from raw post (site_url=%s, sample_keys=%s)",
            len(content_refs),
            site_url,
            [r.key for r in content_refs[:3]],
        )
    else:
        logger.info(
            "shuiyuan: no image refs parsed from raw post for user=%s topic=%s",
            username,
            topic_id,
        )
    effective_message = cleaned_message if content_refs else user_message

    # 尝试获取话题主楼（OP）及标题，用于「当前话题主楼」段落。
    topic_op: Optional[dict] = None
    try:
        topic = client.get_topic(topic_id)
        if topic:
            title = (topic.get("title") or "").strip()
            op_candidates = topic.get("post_stream", {}).get("posts") or []
            for p in op_candidates:
                try:
                    pn = int(p.get("post_number", 0) or 0)
                except Exception:
                    pn = 0
                if pn == 1:
                    topic_op = {
                        "id": p.get("id"),
                        "post_number": pn,
                        "username": p.get("username", ""),
                        "raw": (p.get("raw") or p.get("cooked", "")) or "",
                        "topic_title": title,
                    }
                    break
    except Exception:
        topic_op = None

    # 组装初始上下文：该楼最近 N 条 + 用户聊天历史
    # 若 connector 已传入 thread_posts（topic 监控模式），直接复用，避免重复 get_topic_recent_posts 导致 429
    if thread_posts is not None:
        posts = thread_posts[: cfg.shuiyuan.memory.thread_posts_count]
    else:
        posts = client.get_topic_recent_posts(
            topic_id,
            limit=cfg.shuiyuan.memory.thread_posts_count,
        )

    # 在前端层按业务语气拼装完整上下文 prompt，Core 视为普通 user 输入。
    from .prompt import build_shuiyuan_prompt_from_context

    shuiyuan_ctx: dict = {
        "username": username,
        "topic_id": int(topic_id),
        "reply_to_post_number": reply_to_post_number,
        "reply_to_post_id": reply_to_post_id,
        "topic_op": topic_op,
        "thread_posts": posts,
        # 旧版会话记忆（基于 ShuiyuanDB）已弃用，聊天上下文交由主 Agent 的
        # ChatHistoryDB + 长期记忆系统统一管理，这里不再注入 chat_rows。
    }
    ctx_user = build_shuiyuan_prompt_from_context(
        context=shuiyuan_ctx,
        user_message=effective_message,
    )

    # 优先通过 daemon IPC（per-user 受限 Core），不可用时回退到本地 Agent
    via_daemon = await _run_via_daemon(
        username=username,
        topic_id=topic_id,
        ctx_user=ctx_user,
        reply_to_post_number=reply_to_post_number,
        db=db,
        client=client,
        content_refs=content_refs or None,
    )
    if via_daemon is not None:
        return via_daemon

    # 旧版水源前端 SessionLogger 已弃用，统一由 Kernel/CoreLifecycleLogger 记录。
    session_logger = None
    # 水源 Agent 保留：联网搜索、URL 解析、水源搜索、获取话题（发帖由 automation 层负责）
    # web_search 和 extract_web_content 由 Agent 在 mcp.enabled 时自动注册
    max_posts = getattr(cfg.shuiyuan.memory, "tool_max_posts", 50) or 50
    tools: List[Any] = [
        ShuiyuanSearchTool(config=cfg, max_results=max_posts),
        ShuiyuanGetTopicTool(config=cfg, posts_limit=max_posts),
        ShuiyuanRetortTool(config=cfg),
        AttachImageToReplyTool(),
    ]
    if extra_tools:
        tools.extend(extra_tools)

    async with AgentCore(
        config=cfg,
        tools=tools,
        max_iterations=cfg.agent.max_iterations,
        timezone=cfg.time.timezone,
        user_id=username,
        source="shuiyuan",
        session_logger=session_logger,
    ) as agent:
        # 若存在图片引用，解析为 content_items 注入本轮多模态输入
        content_items: List[Dict[str, Any]] = []
        if content_refs:
            try:
                from agent_core.content import resolve_content_refs

                content_items = await resolve_content_refs(content_refs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("resolve_content_refs failed: %s", exc)

        # 本地 fallback：与 daemon 路径一样，直接使用前端拼装好的 ctx_user。
        output = await agent.process_input(
            ctx_user, content_items=content_items or None
        )
        reply_text = (output or "").strip()

        # 处理 attach_image_to_reply 登记的附件：上传并拼接 Markdown
        attach_md = await _upload_and_embed_attachments(
            agent.get_outgoing_attachments(),
            client=client,
        )
        final_text = reply_text + attach_md

        if final_text:
            success, msg = post_reply(
                username=username,
                topic_id=topic_id,
                raw=final_text,
                reply_to_post_number=reply_to_post_number,
                db=db,
                client=client,
            )
            if not success:
                logger.warning("发帖失败: %s", msg)
        return final_text
