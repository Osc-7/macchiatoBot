"""
水源社区 Connector：轮询 Discourse @ 提及，满足调用规则时触发 run_shuiyuan_reply。

两种模式（由 shuiyuan.allowed_topic_ids 配置）：
1. Topic 监控模式（allowed_topic_ids 非空）：轮询指定 topic 的新帖，解析正文 @owner+trigger
   - 不依赖 user_actions/notifications，可识别自 @
   - 可配置权限：仅在这些 topic 中响应
2. 通知模式（allowed_topic_ids 为空）：user_actions（filter=7、filter=7+acting_username 自 @、filter=5 本人帖）+ notifications
   - 自 @：filter=7+acting_username=主人 + filter=5 兜底；仍可能不如 topic 监控完整

调度：轮询与水位更新尽快返回；``run_shuiyuan_reply`` 以 asyncio Task 后台执行，避免长耗时回复阻塞下一轮提及扫描。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Dict, List, Optional, Set, Tuple

from agent_core.config import Config, get_config

# Discourse user_actions filter: 7 = mentions；5 = 本人发帖/回复流（用于兜底：自 @ 帖会作为「自己的回复」出现）
USER_ACTIONS_FILTER_MENTIONS = 7
USER_ACTIONS_FILTER_MY_POSTS = 5
# notifications notification_type: 1 = mentioned（含自 @）
NOTIFICATION_TYPE_MENTIONED = (1, "1", "mentioned")

# 并发回复上限：不同用户可并行处理，受此限制避免 429 和 daemon 过载
MAX_CONCURRENT_REPLIES = 6
# 收到停止信号后，等待后台回复任务结束的最长时间（秒）
_PENDING_REPLIES_DRAIN_TIMEOUT_SEC = 120.0

logger = logging.getLogger("shuiyuan_connector")


def _schedule_background_reply(
    aw: Awaitable[Any],
    *,
    pending_tasks: Optional[Set[asyncio.Task[Any]]] = None,
) -> None:
    """在事件循环中后台执行回复协程，不阻塞轮询；可选登记到 ``pending_tasks`` 以便退出时排空。"""

    async def _guarded() -> None:
        try:
            await aw
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("水源回复后台任务失败")

    t = asyncio.create_task(_guarded())
    if pending_tasks is not None:
        pending_tasks.add(t)

        def _on_done(task: asyncio.Task[Any]) -> None:
            pending_tasks.discard(task)

        t.add_done_callback(_on_done)


async def _drain_pending_reply_tasks(
    pending: Set[asyncio.Task[Any]],
    *,
    timeout: float = _PENDING_REPLIES_DRAIN_TIMEOUT_SEC,
) -> None:
    """进程退出前等待后台回复结束；超时则 cancel 剩余任务。"""
    if not pending:
        return
    snap = list(pending)
    try:
        await asyncio.wait_for(
            asyncio.gather(*snap, return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "停止 connector 时仍有 %d 个水源回复任务未在 %.0f 秒内结束，将取消",
            len(snap),
            timeout,
        )
        for t in snap:
            if not t.done():
                t.cancel()
        await asyncio.gather(*snap, return_exceptions=True)


_STREAM_MAP_PATH = Path("./data/shuiyuan/connector_stream_map.json")
# 通知模式：已见过的 post_id 快照，避免重启后冷启动仅采 Top60 导致旧帖/自 @ 被误判为「新提及」
_NOTIFY_STREAM_PATH = Path("./data/shuiyuan/connector_notify_stream.json")
_NOTIFY_STREAM_MAX_IDS = 1200


def _load_stream_map() -> Dict[int, Set[int]]:
    """从磁盘加载 topic 监控的 stream_map，避免每次重启都从历史帖子开始初始化。"""
    if not _STREAM_MAP_PATH.is_file():
        return {}
    try:
        text = _STREAM_MAP_PATH.read_text(encoding="utf-8") or "{}"
        raw = json.loads(text)
    except Exception:
        return {}

    out: Dict[int, Set[int]] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                topic_id = int(k)
            except Exception:
                continue
            if isinstance(v, list):
                ids: Set[int] = set()
                for item in v:
                    try:
                        ids.add(int(item))
                    except Exception:
                        continue
                if ids:
                    out[topic_id] = ids
    return out


def _load_notify_stream_list() -> List[int]:
    """加载通知模式已见 post_id 列表（新前旧后，与 _collect 排序一致）。"""
    if not _NOTIFY_STREAM_PATH.is_file():
        return []
    try:
        text = _NOTIFY_STREAM_PATH.read_text(encoding="utf-8") or "{}"
        raw = json.loads(text)
    except Exception:
        return []
    ids = raw.get("post_ids") if isinstance(raw, dict) else raw
    if not isinstance(ids, list):
        return []
    out: List[int] = []
    for x in ids:
        try:
            out.append(int(x))
        except Exception:
            continue
    return out[:_NOTIFY_STREAM_MAX_IDS]


def _save_notify_stream_list(post_ids: List[int]) -> None:
    """持久化通知模式 post_id 快照，重启后继续用同一水位，减少历史误触发。"""
    if not post_ids:
        return
    try:
        _NOTIFY_STREAM_PATH.parent.mkdir(parents=True, exist_ok=True)
        trimmed = [int(x) for x in post_ids[:_NOTIFY_STREAM_MAX_IDS]]
        _NOTIFY_STREAM_PATH.write_text(
            json.dumps({"post_ids": trimmed}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.debug("保存 connector_notify_stream 失败", exc_info=True)


def _save_stream_map(stream_map: Dict[int, Set[int]]) -> None:
    """将 topic 监控的 stream_map 持久化到磁盘，以便下次启动复用。"""
    try:
        _STREAM_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        data: Dict[str, List[int]] = {}
        for topic_id, ids in stream_map.items():
            if not ids:
                continue
            data[str(int(topic_id))] = sorted(int(i) for i in ids)
        _STREAM_MAP_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # 持久化失败不影响主流程，必要时可通过 debug 日志排查
        logger.debug("保存 stream_map 失败", exc_info=True)


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
    from .reply import AUTO_REPLY_MARK

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
            logger.warning(
                "get_topic_recent_posts topic=%s 失败，将跳过本轮: %r", topic_id, e
            )
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
            # 带 raw 的完整帖子（单帖 403 时尝试话题内 posts.json 回退）。
            full_raw = ""
            try:
                post_full = client.get_post_with_read_fallback(topic_id, int(pid))
            except Exception:
                post_full = None
            if isinstance(post_full, dict):
                full_raw = (
                    (post_full.get("raw") or post_full.get("cooked") or "") or ""
                ).strip()
            if not full_raw:
                continue
            # 若完整 raw 中包含自动回复标记，则说明是本 Agent 之前的回复或被引用，直接跳过。
            if AUTO_REPLY_MARK in full_raw:
                continue

            ok, _ = is_invocation_valid_from_raw(full_raw, config=config)
            if ok:
                out.append((int(topic_id), int(pn), int(pid), p))
        stream_map[topic_id] = seen

    out.sort(key=lambda x: x[2], reverse=True)
    return out, stream_map, posts_by_topic


def _collect_mention_post_ids(
    client: Any, config: Config
) -> List[Tuple[int, int, int]]:
    """
    从 user_actions + notifications 收集 (topic_id, post_number, post_id) 列表。

    自 @（自己 @ 自己）：
    - filter=7 默认流往往**不含**自 @，需额外请求 filter=7 且 acting_username=主人（与 client 文档一致）；
    - filter=5 拉取本人发帖/回复，自 @ 帖会作为你的新回复出现；
    - notifications 中 mention 类型作兜底（是否产生取决于站点）。
    """
    owner = (config.shuiyuan.owner_username or "").strip()
    if not owner:
        return []

    seen_post_ids: Set[int] = set()
    out: List[Tuple[int, int, int]] = []

    # 1. user_actions filter=7（他人 @ 你）
    try:
        data = client.get_user_actions(
            owner, filter_type=USER_ACTIONS_FILTER_MENTIONS, offset=0
        )
        actions = data.get("user_actions") or []
        if isinstance(actions, list):
            for a in actions:
                pid = a.get("post_id")
                tid = a.get("topic_id")
                pn = a.get("post_number", 1)
                if (
                    pid is not None
                    and tid is not None
                    and int(pid) not in seen_post_ids
                ):
                    seen_post_ids.add(int(pid))
                    out.append((int(tid), int(pn), int(pid)))
    except Exception as e:
        logger.debug("get_user_actions 失败: %s", e)

    # 1b. filter=7 + acting_username=主人：显式拉取「发帖人即本人」的提及流，覆盖自 @（默认 filter=7 常不含此项）
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
                if (
                    pid is not None
                    and tid is not None
                    and int(pid) not in seen_post_ids
                ):
                    seen_post_ids.add(int(pid))
                    out.append((int(tid), int(pn), int(pid)))
    except Exception as e:
        logger.debug("get_user_actions(自 @ acting_username) 失败: %s", e)

    # 2. user_actions filter=5（本人发帖/回复流：自 @ 帖作为你的新回复出现，与其它发帖共用）
    try:
        data = client.get_user_actions(
            owner,
            filter_type=USER_ACTIONS_FILTER_MY_POSTS,
            offset=0,
        )
        actions = data.get("user_actions") or []
        if isinstance(actions, list):
            for a in actions:
                pid = a.get("post_id")
                tid = a.get("topic_id")
                pn = a.get("post_number", 1)
                if (
                    pid is not None
                    and tid is not None
                    and int(pid) not in seen_post_ids
                ):
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
    *,
    reply_sem: asyncio.Semaphore,
    pending_tasks: Optional[Set[asyncio.Task[Any]]] = None,
) -> tuple[Dict[int, Set[int]], bool]:
    """
    Topic 监控模式轮询一次。仅处理 allowed_topic_ids 中的新帖。

    Returns:
        (更新后的 stream_map, 本轮是否实际触发至少一次回复)
    """
    cfg = config.shuiyuan
    owner = (cfg.owner_username or "").strip()
    if not owner:
        return stream_map, False

    items, stream_map, posts_by_topic = _collect_from_topic_watch(
        client, config, stream_map
    )
    if not items:
        return stream_map, False

    logger.info("发现 %d 条新提及（topic 监控）", len(items))

    from .session import run_shuiyuan_reply

    had_mention = False

    async def _run_one(
        topic_id: int,
        post_number: int,
        post_id: int,
        post: dict,
    ) -> bool:
        if not topic_id or not post_id:
            return False
        async with reply_sem:
            await asyncio.sleep(1.0)  # 降低 429 风险
            raw = (post.get("raw") or post.get("cooked") or "").strip()
            username = (post.get("username") or "").strip()
            logger.info(
                "触发水源回复 topic=%s post=%s user=%s", topic_id, post_number, username
            )
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
                    return True
                logger.warning("水源回复返回空")
                return True  # 仍视为有活动
            except Exception as e:
                logger.exception("水源回复失败: %s", e)
                return True  # 异常也视为有活动（已尝试处理）

    for topic_id, post_number, post_id, post in items:
        if not topic_id or not post_id:
            continue
        had_mention = True
        _schedule_background_reply(
            _run_one(int(topic_id), int(post_number), int(post_id), post),
            pending_tasks=pending_tasks,
        )

    return stream_map, had_mention


async def _poll_once(
    client: Any,
    config: Config,
    stream_list: List[int],
    *,
    reply_sem: asyncio.Semaphore,
    pending_tasks: Optional[Set[asyncio.Task[Any]]] = None,
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
        logger.info(
            "首次运行，初始化 stream_list（%d 条），不处理历史", len(new_stream)
        )
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

    from .session import is_invocation_valid_from_raw, run_shuiyuan_reply

    async def _run_one(topic_id: int, post_number: int, post_id: int) -> None:
        if not topic_id or not post_id:
            return
        async with reply_sem:
            await asyncio.sleep(0.6)
            try:
                post = await asyncio.to_thread(
                    client.get_post_with_read_fallback,
                    topic_id,
                    int(post_id),
                )
            except Exception as e:
                logger.warning(
                    "获取帖子失败 topic=%s post_id=%s: %s", topic_id, post_id, e
                )
                return

            if not post:
                logger.warning(
                    "无法获取帖子 topic=%s post_id=%s（404、403 受限版块或仅站内可见时常见）",
                    topic_id,
                    post_id,
                )
                return

            raw = (post.get("raw") or post.get("cooked") or "").strip()
            username = (post.get("username") or "").strip()

            ok, reason = is_invocation_valid_from_raw(raw, config=config)
            if not ok:
                logger.debug("跳过不满足规则 post_id=%s: %s", post_id, reason)
                return

            logger.info(
                "触发水源回复 topic=%s post=%s user=%s",
                topic_id,
                post_number,
                username,
            )
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

    for topic_id, post_number, post_id in new_items:
        if not topic_id or not post_id:
            continue
        _schedule_background_reply(
            _run_one(int(topic_id), int(post_number), int(post_id)),
            pending_tasks=pending_tasks,
        )

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
    # 跨轮询共享：避免每轮新建 Semaphore 导致并发失控
    reply_sem = asyncio.Semaphore(MAX_CONCURRENT_REPLIES)
    pending_reply_tasks: Set[asyncio.Task[Any]] = set()

    if allowed:
        # 加载历史 stream_map，避免每次重启都从历史帖子重新初始化
        stream_map: Dict[int, Set[int]] = _load_stream_map()
        # 动态轮询节奏：根据最近是否有 @ 活动选择快/中/慢轮询
        last_mention_ts: float | None = None
        # 快速轮询：刚有人 @ 时，优先降低响应延迟
        fast_interval = min(5.0, poll_interval_seconds / 2.0)
        # 正常轮询：作为默认节奏
        normal_interval = float(poll_interval_seconds)
        # 慢速轮询：长时间无人 @ 时，降低日级总请求量
        slow_interval = max(normal_interval * 3.0, normal_interval)
        # 认为「最近有活动」的时间窗口（分钟）
        active_window_minutes = 10.0
        # 超过此时间仍无活动则视为「非常安静」
        quiet_window_minutes = 60.0
        backoff_until_topic: float = 0.0
        logger.info(
            "水源 connector 启动（topic 监控），owner=%s，topics=%s，轮询间隔 %s 秒",
            owner,
            allowed,
            poll_interval_seconds,
        )
        while not stop.is_set():
            # 若之前收到 Retry-After 等限流提示，则在冷却期内跳过主动轮询
            now = time.time()
            if now < backoff_until_topic:
                wait_secs = max(0.0, backoff_until_topic - now)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=wait_secs)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                stream_map, had_mention = await _poll_topic_watch(
                    client,
                    cfg,
                    stream_map,
                    reply_sem=reply_sem,
                    pending_tasks=pending_reply_tasks,
                )
                _save_stream_map(stream_map)
                if had_mention:
                    last_mention_ts = time.time()
            except Exception as e:
                # 特判限流异常，打印更详细信息
                from .client import ShuiyuanRateLimitError

                if isinstance(e, ShuiyuanRateLimitError):
                    headers = getattr(e, "headers", None) or {}
                    retry_after = headers.get("Retry-After") or headers.get(
                        "retry-after"
                    )
                    if retry_after is not None:
                        try:
                            delay = float(retry_after)
                        except Exception:
                            delay = poll_interval_seconds * 3.0
                    else:
                        # 若无 Retry-After，则退避为 3 倍轮询间隔
                        delay = poll_interval_seconds * 3.0
                    backoff_until_topic = max(
                        backoff_until_topic,
                        time.time() + max(delay, poll_interval_seconds),
                    )
                    logger.warning(
                        "轮询限流(429)：%s (path=%s, headers=%s)，将在 %.0f 秒后重试",
                        getattr(e, "body_preview", ""),
                        getattr(e, "path", ""),
                        _safe_headers_for_log(getattr(e, "headers", None)),
                        delay,
                    )
                else:
                    logger.exception("轮询异常: %s", e)
            try:
                # 根据最近一次 @ 活动时间，动态选择下次轮询间隔
                now = time.time()
                if last_mention_ts is None:
                    interval = normal_interval
                else:
                    delta_min = (now - last_mention_ts) / 60.0
                    if delta_min <= active_window_minutes:
                        interval = fast_interval
                    elif delta_min <= quiet_window_minutes:
                        interval = normal_interval
                    else:
                        interval = slow_interval
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        await _drain_pending_reply_tasks(pending_reply_tasks)
        return

    # 通知模式：user_actions + notifications
    stream_list: List[int] = _load_notify_stream_list()
    backoff_until_notify: float = 0.0
    if stream_list:
        logger.info(
            "水源 connector 启动（通知模式），已加载 %s 条历史 post_id 水位（%s）",
            len(stream_list),
            _NOTIFY_STREAM_PATH,
        )
    logger.info(
        "水源 connector 启动（user_actions filter=7），owner=%s，轮询间隔 %s 秒",
        owner,
        poll_interval_seconds,
    )
    while not stop.is_set():
        try:
            stream_list = await _poll_once(
                client,
                cfg,
                stream_list,
                reply_sem=reply_sem,
                pending_tasks=pending_reply_tasks,
            )
            _save_notify_stream_list(stream_list)
        except Exception as e:
            from .client import ShuiyuanRateLimitError

            if isinstance(e, ShuiyuanRateLimitError):
                headers = getattr(e, "headers", None) or {}
                retry_after = headers.get("Retry-After") or headers.get("retry-after")
                if retry_after is not None:
                    try:
                        delay = float(retry_after)
                    except Exception:
                        delay = poll_interval_seconds * 3.0
                else:
                    delay = poll_interval_seconds * 3.0
                backoff_until_notify = max(
                    backoff_until_notify,
                    time.time() + max(delay, poll_interval_seconds),
                )
                logger.warning(
                    "轮询限流(429)：%s (path=%s, headers=%s)，将在 %.0f 秒后重试",
                    getattr(e, "body_preview", ""),
                    getattr(e, "path", ""),
                    _safe_headers_for_log(getattr(e, "headers", None)),
                    delay,
                )
            else:
                logger.exception("轮询异常: %s", e)
        try:
            now = time.time()
            # 如果处于限流冷却期，则优先等待冷却结束；否则按正常轮询间隔等待
            if now < backoff_until_notify:
                wait_secs = max(0.0, backoff_until_notify - now)
            else:
                wait_secs = poll_interval_seconds
            await asyncio.wait_for(stop.wait(), timeout=wait_secs)
        except asyncio.TimeoutError:
            pass
    await _drain_pending_reply_tasks(pending_reply_tasks)


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

    asyncio.run(run_connector_loop(poll_interval_seconds=40, stop_event=stop))


if __name__ == "__main__":
    main()
