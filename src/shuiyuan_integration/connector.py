"""
水源社区 Connector：轮询 Discourse @ 提及，满足调用规则时触发 run_shuiyuan_reply。

两种模式（由 shuiyuan.allowed_topic_ids 配置）：
1. Topic 监控模式（allowed_topic_ids 非空）：轮询指定 topic 的新帖，解析正文 @owner+trigger
   - 不依赖 user_actions/notifications，可识别自 @
   - 可配置权限：仅在这些 topic 中响应
2. 通知模式（allowed_topic_ids 为空）：轮询 user_actions + notifications
   - 可能漏掉自 @
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.config import Config, get_config

# Discourse user_actions filter: 7 = mentions
USER_ACTIONS_FILTER_MENTIONS = 7
# notifications notification_type: 1 = mentioned（含自 @）
NOTIFICATION_TYPE_MENTIONED = (1, "1", "mentioned")

logger = logging.getLogger("shuiyuan_connector")


def _safe_headers_for_log(headers: Any) -> dict:
    """将响应头转换为适合日志输出的精简 dict，避免巨大输出。"""
    if not headers:
        return {}
    try:
        items = dict(headers).items()
    except Exception:
        return {}
    # 只保留与限流相关的关键字段
    keys = {
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
        "Retry-After",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "retry-after",
    }
    return {k: v for k, v in items if k in keys}


def _collect_from_topic_watch(
    client: Any,
    config: Config,
    stream_map: Dict[int, Set[int]],
) -> Tuple[List[Tuple[int, int, int, dict]], Dict[int, Set[int]], Dict[int, list]]:
    """
    Topic 监控模式：从指定 topic 的新帖中收集 (topic_id, post_number, post_id, post_dict)。
    仅返回正文含 @owner 且含 trigger 的帖子。首次运行仅初始化 stream_map，不处理历史。
    返回 post_dict 避免后续再调 get_post_by_id，并返回 posts_by_topic 供 run_shuiyuan_reply
    复用，避免重复 get_topic_recent_posts 导致 429 限流。

    Returns:
        (待处理的 items，更新后的 stream_map，topic_id -> posts 映射)
    """
    cfg = config.shuiyuan
    topic_ids = cfg.allowed_topic_ids or []
    if not topic_ids:
        return [], stream_map, {}

    from .session import is_invocation_valid_from_raw

    out: List[Tuple[int, int, int, dict]] = []
    posts_by_topic: Dict[int, list] = {}
    for topic_id in topic_ids:
        seen = stream_map.get(topic_id) or set()
        is_first = len(seen) == 0
        if is_first:
            logger.info("topic %s 首次运行，初始化 stream_map", topic_id)
        try:
            posts = client.get_topic_recent_posts(topic_id, limit=50)
        except Exception as e:
            # 使用 warning 级别打印，避免异常静默导致看起来像“卡住”
            logger.warning("get_topic_recent_posts topic=%s 失败，将跳过本轮: %r", topic_id, e)
            continue

    posts_by_topic[topic_id] = posts
    for p in posts:
        pid = p.get("id")
        pn = p.get("post_number", 1)
        if pid is None or pid in seen:
            continue
        seen.add(int(pid))
        if is_first:
            continue
        raw = (p.get("raw") or p.get("cooked") or "").strip()
        ok, _ = is_invocation_valid_from_raw(raw, config=config)
        if ok:
            out.append((int(topic_id), int(pn), int(pid), p))
    stream_map[topic_id] = seen

    out.sort(key=lambda x: x[2], reverse=True)
    return out, stream_map, posts_by_topic


def _collect_mention_post_ids(client: Any, config: Config) -> List[Tuple[int, int, int]]:
    """
    从 user_actions + notifications 收集 (topic_id, post_number, post_id) 列表。
    notifications 可能包含自 @，user_actions 通常不含。
    """
    owner = (config.shuiyuan.owner_username or "").strip()
    if not owner:
        return []

    seen_post_ids: Set[int] = set()
    out: List[Tuple[int, int, int]] = []

    # 1. user_actions filter=7（他人 @ 你）
    try:
        data = client.get_user_actions(owner, filter_type=USER_ACTIONS_FILTER_MENTIONS, offset=0)
        actions = data.get("user_actions") or []
        if isinstance(actions, list):
            for a in actions:
                pid = a.get("post_id")
                tid = a.get("topic_id")
                pn = a.get("post_number", 1)
                if pid is not None and tid is not None and int(pid) not in seen_post_ids:
                    seen_post_ids.add(int(pid))
                    out.append((int(tid), int(pn), int(pid)))
    except Exception as e:
        logger.debug("get_user_actions 失败: %s", e)

    # 2. user_actions filter=7&acting_username=owner（自 @：显式请求）
    try:
        data = client.get_user_actions(
            owner,
            filter_type=USER_ACTIONS_FILTER_MENTIONS,
            offset=0,
            acting_username=owner,
        )
        actions = data.get("user_actions") or []
        if isinstance(actions, list):
            for a in actions:
                pid = a.get("post_id")
                tid = a.get("topic_id")
                pn = a.get("post_number", 1)
                if pid is not None and tid is not None and int(pid) not in seen_post_ids:
                    seen_post_ids.add(int(pid))
                    out.append((int(tid), int(pn), int(pid)))
    except Exception as e:
        logger.debug("get_user_actions(acting_username=owner) 失败: %s", e)

    # 3. notifications（兜底）
    try:
        data = client.get_notifications(limit=30, offset=0)
        notifications = data.get("notifications") or []
        if isinstance(notifications, dict):
            notifications = list(notifications.values()) if notifications else []
        for n in notifications:
            if n.get("notification_type") not in NOTIFICATION_TYPE_MENTIONED:
                continue
            tid = n.get("topic_id")
            pn = n.get("post_number", 1)
            if not tid:
                continue
            # 尝试从 data 或通过 get_post_by_number 获取 post_id
            post_id: Optional[int] = None
            d = n.get("data")
            if isinstance(d, dict):
                raw_id = d.get("original_post_id") or d.get("post_id")
                if raw_id is not None:
                    post_id = int(raw_id)
            elif isinstance(d, str):
                try:
                    import json
                    parsed = json.loads(d) if d else {}
                    if isinstance(parsed, dict):
                        raw_id = parsed.get("original_post_id") or parsed.get("post_id")
                        if raw_id is not None:
                            post_id = int(raw_id)
                except Exception:
                    pass
            if post_id is None:
                try:
                    post = client.get_post_by_number(tid, pn)
                    if post and post.get("id") is not None:
                        post_id = int(post["id"])
                except Exception:
                    pass
            if post_id is not None and post_id not in seen_post_ids:
                seen_post_ids.add(post_id)
                out.append((int(tid), int(pn), post_id))
    except Exception as e:
        logger.debug("get_notifications 失败: %s", e)

    # 按 post_id 降序（post_id 越大越新），保持 stream diff 一致
    out.sort(key=lambda x: x[2], reverse=True)
    return out


async def _poll_topic_watch(
    client: Any,
    config: Config,
    stream_map: Dict[int, Set[int]],
) -> Dict[int, Set[int]]:
    """
    Topic 监控模式轮询一次。仅处理 allowed_topic_ids 中的新帖。
    """
    cfg = config.shuiyuan
    owner = (cfg.owner_username or "").strip()
    if not owner:
        return stream_map

    items, stream_map, posts_by_topic = _collect_from_topic_watch(client, config, stream_map)
    if not items:
        return stream_map

    logger.info("发现 %d 条新提及（topic 监控）", len(items))

    from .session import run_shuiyuan_reply

    for topic_id, post_number, post_id, post in items:
        if not topic_id or not post_id:
            continue
        await asyncio.sleep(1.0)  # 降低 429 风险
        raw = (post.get("raw") or post.get("cooked") or "").strip()
        username = (post.get("username") or "").strip()
        logger.info("触发水源回复 topic=%s post=%s user=%s", topic_id, post_number, username)
        thread_posts = posts_by_topic.get(topic_id) if posts_by_topic else None
        try:
            result = await run_shuiyuan_reply(
                username=username,
                topic_id=int(topic_id),
                user_message=raw,
                reply_to_post_number=int(post_number) if post_number else None,
                reply_to_post_id=int(post_id) if post_id else None,
                config=config,
                thread_posts=thread_posts,
            )
            if result:
                logger.info("水源回复完成")
            else:
                logger.warning("水源回复返回空")
        except Exception as e:
            logger.exception("水源回复失败: %s", e)

    return stream_map


async def _poll_once(
    client: Any,
    config: Config,
    stream_list: List[int],
) -> List[int]:
    """
    轮询一次，仅处理新增提及（user_actions + notifications，含自 @）。

    Returns:
        新的 stream_list（post_id 列表，newest first）
    """
    cfg = config.shuiyuan
    owner = (cfg.owner_username or "").strip()
    if not owner:
        logger.warning("未配置 owner_username")
        return stream_list

    items = _collect_mention_post_ids(client, config)
    new_stream = [post_id for _, _, post_id in items]
    if not new_stream:
        return stream_list

    # ShuiyuanAutoReply 逻辑：首次运行只初始化，不处理
    if not stream_list:
        logger.info("首次运行，初始化 stream_list（%d 条），不处理历史", len(new_stream))
        return new_stream

    # 找到 overlap：第一个已在 stream_list 中的 post_id 的索引
    last_post_index = len(new_stream)
    for i, pid in enumerate(new_stream):
        if pid in stream_list:
            last_post_index = i
            break

    new_items = items[:last_post_index]
    if not new_items:
        return new_stream

    logger.info("发现 %d 条新提及", len(new_items))

    for topic_id, post_number, post_id in new_items:
        if not topic_id or not post_id:
            continue

        await asyncio.sleep(0.6)
        try:
            post = client.get_post_by_id(topic_id, int(post_id))
        except Exception as e:
            logger.warning("获取帖子失败 topic=%s post_id=%s: %s", topic_id, post_id, e)
            continue

        if not post:
            logger.warning("无法获取帖子 topic=%s post_id=%s", topic_id, post_id)
            continue

        raw = (post.get("raw") or post.get("cooked") or "").strip()
        username = (post.get("username") or "").strip()
        # user_actions filter=7 即 mentions，owner 被 @ 了
        mentioned_usernames: List[str] = [owner]

        from .session import is_invocation_valid, run_shuiyuan_reply

        ok, reason = is_invocation_valid(raw, mentioned_usernames, config=config)
        if not ok:
            logger.debug("跳过不满足规则 post_id=%s: %s", post_id, reason)
            continue

        logger.info("触发水源回复 topic=%s post=%s user=%s", topic_id, post_number, username)
        try:
            result = await run_shuiyuan_reply(
                username=username,
                topic_id=int(topic_id),
                user_message=raw,
                reply_to_post_number=int(post_number) if post_number else None,
                reply_to_post_id=int(post_id) if post_id else None,
                config=config,
            )
            if result:
                logger.info("水源回复完成")
            else:
                logger.warning("水源回复返回空")
        except Exception as e:
            logger.exception("水源回复失败: %s", e)

    return new_stream


async def run_connector_loop(
    config: Optional[Config] = None,
    poll_interval_seconds: float = 30.0,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """
    轮询水源通知，满足规则时调用 run_shuiyuan_reply。

    Args:
        config: 配置对象
        poll_interval_seconds: 轮询间隔（秒）
        stop_event: 设置时停止轮询
    """
    cfg = config or get_config()
    if not cfg.shuiyuan.enabled:
        logger.info("水源未启用，connector 退出")
        return

    try:
        from . import get_shuiyuan_client_from_config
    except ImportError as e:
        logger.error("无法加载水源集成: %s", e)
        return

    client = get_shuiyuan_client_from_config(cfg)
    if not client:
        logger.error("水源未配置 User-Api-Key，connector 退出")
        return

    owner = (cfg.shuiyuan.owner_username or "").strip()
    if not owner:
        logger.error("未配置 shuiyuan.owner_username，connector 退出")
        return

    allowed = cfg.shuiyuan.allowed_topic_ids or []
    stop = stop_event or asyncio.Event()

    if allowed:
        stream_map: Dict[int, Set[int]] = {}
        logger.info(
            "水源 connector 启动（topic 监控），owner=%s，topics=%s，轮询间隔 %s 秒",
            owner,
            allowed,
            poll_interval_seconds,
        )
        while not stop.is_set():
            try:
                stream_map = await _poll_topic_watch(client, cfg, stream_map)
            except Exception as e:
                # 特判限流异常，打印更详细信息
                from .client import ShuiyuanRateLimitError

                if isinstance(e, ShuiyuanRateLimitError):
                    logger.warning(
                        "轮询限流(429)：%s (path=%s, headers=%s)",
                        getattr(e, "body_preview", ""),
                        getattr(e, "path", ""),
                        _safe_headers_for_log(getattr(e, "headers", None)),
                    )
                else:
                    logger.exception("轮询异常: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
        return

    # 通知模式：user_actions + notifications
    stream_list: List[int] = []
    logger.info(
        "水源 connector 启动（user_actions filter=7），owner=%s，轮询间隔 %s 秒",
        owner,
        poll_interval_seconds,
    )
    while not stop.is_set():
        try:
            stream_list = await _poll_once(client, cfg, stream_list)
        except Exception as e:
            from .client import ShuiyuanRateLimitError

            if isinstance(e, ShuiyuanRateLimitError):
                logger.warning(
                    "轮询限流(429)：%s (path=%s, headers=%s)",
                    getattr(e, "body_preview", ""),
                    getattr(e, "path", ""),
                    _safe_headers_for_log(getattr(e, "headers", None)),
                )
            else:
                logger.exception("轮询异常: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval_seconds)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    """CLI 入口：后台轮询水源通知。"""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # 需在项目根目录运行：source init.sh 或 PYTHONPATH=src python -m shuiyuan_integration.connector
    stop = asyncio.Event()

    def _on_sig(*_args: object) -> None:
        stop.set()

    try:
        import signal

        signal.signal(signal.SIGINT, _on_sig)
        signal.signal(signal.SIGTERM, _on_sig)
    except Exception:
        pass

    asyncio.run(run_connector_loop(poll_interval_seconds=10, stop_event=stop))


if __name__ == "__main__":
    main()
