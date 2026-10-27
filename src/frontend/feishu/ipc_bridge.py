from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from agent_core.config import get_config
from agent_core.interfaces import AgentRunInput, AgentRunResult
from system.automation import AutomationIPCClient, default_socket_path

from .client import FeishuClient
from .feishu_turn_hooks import FeishuTurnHooksController
from .reply_dispatch import send_feishu_agent_final_reply
from .session_override import resolve_session_override, set_session_override
from .slash_commands import try_handle_slash_command

"""
Automation IPC Bridge for Feishu.

封装 AutomationIPCClient，提供面向飞书前端的简单消息发送接口。
支持斜杠指令（/clear、/interrupt、/usage、/session、/help）与 CLI 对齐。

FeishuPushForwarder：后台轮询 [out] 队列，将 inject_turn 等推送结果发回飞书。
"""


logger = logging.getLogger(__name__)


def _default_feishu_chat_store_path() -> Path:
    """私聊 session 到 chat_id 的落盘位置。只属于飞书进程，kernel 不读这个文件。"""
    test_dir = os.environ.get("SCHEDULE_AGENT_TEST_DATA_DIR")
    if test_dir:
        return Path(test_dir) / "feishu" / "session_chats.json"
    return Path("data") / "feishu" / "session_chats.json"


# 全局 push 转发器，供 ws_client 注册会话并启动
_feishu_push_forwarder: Optional["FeishuPushForwarder"] = None
_forwarder_lock = threading.Lock()


def get_feishu_push_forwarder() -> "FeishuPushForwarder":
    """获取或创建 Feishu push 转发器（单例）。"""
    global _feishu_push_forwarder
    with _forwarder_lock:
        if _feishu_push_forwarder is None:
            _feishu_push_forwarder = FeishuPushForwarder()
        return _feishu_push_forwarder


def register_feishu_push_session(
    session_id: str, chat_id: str, ttl_seconds: float = 300.0
) -> None:
    """记下这个会话发回哪个聊天，并确保转发循环在跑。

    ttl_seconds 不再决定要不要去取队列。结果留在 kernel 队列里，直到本进程确认送达。
    """
    del ttl_seconds
    remember_feishu_chat(session_id, chat_id)


def remember_feishu_chat(session_id: str, chat_id: str) -> None:
    """记下 session 发回哪个聊天，并落盘。进程重启后私聊仍能对上。"""
    forwarder = get_feishu_push_forwarder()
    forwarder.remember_chat(session_id, chat_id)
    forwarder.start()


class AutomationDaemonUnavailable(RuntimeError):
    """当 automation daemon 未运行或 IPC 连接失败时抛出。"""


MSG_FEISHU_DAEMON_UNAVAILABLE = (
    "无法连接本地 automation 服务（macchiato agent），请确认已启动 automation_daemon。"
)


def _extract_override_session_target(text: str) -> tuple[Optional[str], bool]:
    """解析会改变当前会话绑定的斜杠命令目标 session_id。

    Returns:
        (target, needs_active_fallback)
    """
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None, False
    parts = raw[1:].strip().split()
    if not parts:
        return None, False
    cmd = parts[0].lower()
    if cmd == "new":
        if len(parts) > 1 and parts[1].strip():
            return parts[1].strip(), False
        return None, True
    if cmd != "session" or len(parts) < 2:
        return None, False
    sub = parts[1].lower()
    if sub in ("new", "switch"):
        if len(parts) > 2 and parts[2].strip():
            return parts[2].strip(), False
        return None, sub == "new"
    return None, False


def _default_feishu_session_id(
    *,
    chat_type: str,
    chat_id: str,
    open_id: str,
    user_id: str,
) -> str:
    chat_type = (chat_type or "").strip() or "p2p"
    chat_id = (chat_id or "").strip()
    open_id = (open_id or "").strip()
    user_id = (user_id or "").strip()
    if chat_type == "p2p":
        key = open_id or user_id or chat_id or "unknown"
        return f"feishu:user:{key}"
    key = chat_id or open_id or user_id or "unknown"
    return f"feishu:chat:{key}"


def format_feishu_processing_error(exc: BaseException) -> str:
    """将异常格式化为飞书用户可见的一行说明（避免过长）。"""
    msg = str(exc).strip() or type(exc).__name__
    if len(msg) > 500:
        msg = msg[:499] + "…"
    return f"处理消息时出错：{msg}"


async def send_classified_feishu_error(
    *,
    chat_id: str,
    error_info: Dict[str, Any],
    timeout_seconds: float = 30.0,
) -> bool:
    """把结构化错误发成说明卡片。发送失败返回 False，调用方再决定是否改发纯文本。"""
    cid = (chat_id or "").strip()
    if not cid or not isinstance(error_info, dict):
        return False
    try:
        from system.kernel.error_notify import (
            build_user_facing_error_card,
            error_info_from_dict,
        )

        client = FeishuClient(timeout_seconds=timeout_seconds)
        await client.send_interactive_card(
            chat_id=cid,
            card=build_user_facing_error_card(error_info_from_dict(error_info)),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("飞书错误卡片发送失败 (chat_id=%s): %s", cid, exc)
        return False


async def send_feishu_error_notice(
    *,
    chat_id: str,
    text: str,
    timeout_seconds: float = 30.0,
) -> None:
    """向飞书会话发送一条用户可见的错误或状态提示。"""
    cid = (chat_id or "").strip()
    if not cid or not (text or "").strip():
        return
    try:
        client = FeishuClient(timeout_seconds=timeout_seconds)
        await client.send_text_message(chat_id=cid, text=text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("send_feishu_error_notice failed (chat_id=%s): %s", cid, exc)


async def try_handle_slash_command_via_ipc(
    *,
    session_id: str,
    text: str,
    socket_path: Optional[str] = None,
    timeout_seconds: float = 120.0,
    owner_id: str = "root",
    source: str = "feishu",
    feishu_chat_type: str = "p2p",
    feishu_chat_id: str = "",
    feishu_open_id: str = "",
    feishu_user_id: str = "",
) -> Optional[str]:
    """
    尝试处理斜杠指令（/clear、/usage、/session、/help）。

    Returns:
        若为斜杠指令则返回回复文本，否则返回 None
    """
    client = AutomationIPCClient(
        owner_id=owner_id,
        source=source,
        socket_path=socket_path or default_socket_path(),
        timeout_seconds=timeout_seconds,
    )
    if not await client.ping():
        raise AutomationDaemonUnavailable(
            f"automation daemon is not reachable via IPC socket: {socket_path or default_socket_path()}"
        )
    base_session = _default_feishu_session_id(
        chat_type=feishu_chat_type,
        chat_id=feishu_chat_id,
        open_id=feishu_open_id,
        user_id=feishu_user_id,
    )
    effective_session = (
        resolve_session_override(
            chat_type=feishu_chat_type,
            chat_id=feishu_chat_id,
            open_id=feishu_open_id,
            user_id=feishu_user_id,
        )
        or session_id
    )
    setattr(client, "feishu_base_session_id", base_session or session_id)
    setattr(client, "feishu_open_id", (feishu_open_id or "").strip())
    setattr(client, "feishu_user_id", (feishu_user_id or "").strip())
    setattr(client, "feishu_chat_id", (feishu_chat_id or "").strip())
    setattr(client, "session_list_limit", 30)
    await client.switch_session(effective_session, create_if_missing=True)
    handled, reply = await try_handle_slash_command(client, text)
    if handled:
        target, needs_active_fallback = _extract_override_session_target(text)
        if target:
            set_session_override(
                chat_type=feishu_chat_type,
                chat_id=feishu_chat_id,
                open_id=feishu_open_id,
                user_id=feishu_user_id,
                session_id=target,
            )
        elif needs_active_fallback:
            active = getattr(client, "active_session_id", "")
            if isinstance(active, str) and active.strip():
                set_session_override(
                    chat_type=feishu_chat_type,
                    chat_id=feishu_chat_id,
                    open_id=feishu_open_id,
                    user_id=feishu_user_id,
                    session_id=active.strip(),
                )
        # /goal 等斜杠命令会 inject_turn 异步执行；须注册 push 转发，否则结果不会回到飞书。
        chat_id = (feishu_chat_id or "").strip()
        active_sid = str(
            getattr(client, "active_session_id", "") or effective_session
        ).strip()
        if chat_id and active_sid:
            get_feishu_push_forwarder().start()
            # Goal 可能长时间运行；与 agent_wake 一致保持 push 转发注册
            is_goal = text.strip().lower().startswith("/goal")
            register_feishu_push_session(
                session_id=active_sid,
                chat_id=chat_id,
                ttl_seconds=3600.0 if is_goal else 600.0,
            )
    return reply if handled else None


class FeishuIPCBridge:
    """飞书到 automation daemon 的 IPC 桥。"""

    def __init__(
        self,
        *,
        socket_path: Optional[str] = None,
        ipc_timeout_seconds: Optional[float] = None,
    ) -> None:
        self._socket_path = socket_path or default_socket_path()
        if ipc_timeout_seconds is not None:
            self._ipc_timeout_seconds = float(ipc_timeout_seconds)
        else:
            self._ipc_timeout_seconds = float(
                get_config().feishu.automation_ipc_timeout_seconds
            )

    async def send_message(
        self,
        *,
        session_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        owner_id: str = "root",
        source: str = "feishu",
    ) -> AgentRunResult:
        """
        将一条飞书消息转发给 automation daemon，并获取 Agent 的最终响应。

        同时基于 trace 事件在飞书侧输出「多轮推理中间状态」，与 CLI 的
        多轮工具调用展示保持一致（但不暴露思维链 reasoning token）。

        Args:
            session_id: macchiatoBot 会话 ID（已根据飞书会话映射）
            text: 输入的自然语言消息
            metadata: 附加到 AgentRunInput.metadata 的信息（可包含飞书 open_id/chat_id 等）
            owner_id: 逻辑用户 ID，目前主要用于日志标识
            source: 来源标识，固定为 "feishu"
        """
        client = AutomationIPCClient(
            owner_id=owner_id,
            source=source,
            socket_path=self._socket_path,
            timeout_seconds=self._ipc_timeout_seconds,
        )

        # 快速探测 daemon 是否在线
        if not await client.ping():
            raise AutomationDaemonUnavailable(
                f"automation daemon is not reachable via IPC socket: {self._socket_path}"
            )

        # 切换/创建对应会话
        await client.switch_session(session_id, create_if_missing=True)

        meta_dict: Dict[str, Any] = dict(metadata or {})
        chat_id = str(meta_dict.get("feishu_chat_id") or "").strip()
        if not chat_id:
            agent_input = AgentRunInput(text=text, metadata=meta_dict)
            return await client.run_turn(agent_input, hooks=None)

        fei = get_config().feishu
        feishu_http_timeout = max(float(fei.timeout_seconds), 120.0)
        ctrl = FeishuTurnHooksController(
            chat_id=chat_id,
            timeout_seconds=feishu_http_timeout,
            markdown_header_title="回复",
        )
        agent_input = AgentRunInput(text=text, metadata=meta_dict)
        result = await client.run_turn(agent_input, hooks=ctrl.hooks)
        return await ctrl.finalize_after_run(result)


def _push_payload_needs_delivery(payload: Dict[str, Any]) -> bool:
    """这条推送如果没发出去，就还得留在队列里。空结果直接丢掉。"""
    if not isinstance(payload, dict):
        return False
    meta = payload.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    if meta.get("_error_info"):
        return True
    attachments = payload.get("attachments")
    if isinstance(attachments, list) and attachments:
        return True
    text = str(payload.get("output_text") or "").strip()
    if text and not meta.get("feishu_skip_final_reply"):
        return True
    return False


class FeishuPushForwarder:
    """后台领取 kernel 交给本前端的队列结果，发回对应的飞书聊天。

    结果在确认送达前留在 kernel 队列里。聊天地址记在本进程，并落到
    data/feishu/session_chats.json，私聊在进程重启后仍能对上。
    """

    def __init__(
        self,
        *,
        poll_interval_seconds: float = 2.0,
        socket_path: Optional[str] = None,
        timeout_seconds: float = 30.0,
        chat_store_path: Optional[str] = None,
    ) -> None:
        self._poll_interval = poll_interval_seconds
        self._socket_path = socket_path or default_socket_path()
        self._timeout = timeout_seconds
        self._chat_store_path = (
            Path(chat_store_path)
            if chat_store_path is not None
            else _default_feishu_chat_store_path()
        )
        self._chat_by_session: Dict[str, str] = self._load_chats()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _load_chats(self) -> Dict[str, str]:
        path = self._chat_store_path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("feishu session chat store unreadable: %s", exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        loaded: Dict[str, str] = {}
        for key, value in raw.items():
            sid = str(key or "").strip()
            cid = str(value or "").strip()
            if sid and cid:
                loaded[sid] = cid
        return loaded

    def _save_chats_locked(self) -> None:
        path = self._chat_store_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._chat_by_session, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("feishu session chat store write failed: %s", exc)

    def remember_chat(self, session_id: str, chat_id: str) -> None:
        """记住这个会话的飞书聊天，并落盘。不开始轮询。"""
        sid = (session_id or "").strip()
        cid = (chat_id or "").strip()
        if not sid or not cid:
            return
        with self._lock:
            if self._chat_by_session.get(sid) == cid:
                return
            self._chat_by_session[sid] = cid
            self._save_chats_locked()

    def chat_id_for_session(self, session_id: str) -> str:
        """本进程见过的聊天，或群会话 id 里自带的 chat_id。"""
        sid = (session_id or "").strip()
        if not sid:
            return ""
        with self._lock:
            known = (self._chat_by_session.get(sid) or "").strip()
        if known:
            return known
        if sid.startswith("feishu:chat:"):
            rest = sid.split(":", 2)
            if len(rest) >= 3:
                return rest[2].strip()
        return ""

    def register(
        self, session_id: str, chat_id: str, ttl_seconds: Optional[float] = None
    ) -> None:
        """记下会话对应的聊天。ttl_seconds 不再控制投递。"""
        del ttl_seconds
        self.remember_chat(session_id, chat_id)

    def start(self) -> None:
        """启动后台轮询线程。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="feishu-push-forwarder",
                daemon=True,
            )
            self._thread.start()
            logger.info("FeishuPushForwarder started")

    def _run_loop(self) -> None:
        """轮询循环（在独立线程中运行，内部创建新事件循环）。"""
        while not self._stop.wait(timeout=self._poll_interval):
            try:
                asyncio.run(self._poll_once())
            except Exception as exc:
                logger.warning("FeishuPushForwarder poll_once failed: %s", exc)

    async def _poll_once(self) -> None:
        """领取本前端的队列结果。发出去之后确认，发不出去就解除租约。"""
        try:
            client = AutomationIPCClient(
                owner_id="root",
                source="feishu",
                socket_path=self._socket_path,
                timeout_seconds=self._timeout,
            )
            if not await client.ping():
                return
            try:
                from agent_core.tools.agent_wake import (
                    load_feishu_push_watch_targets_from_file,
                )

                for item in load_feishu_push_watch_targets_from_file():
                    sid = str(item.get("session_id") or "").strip()
                    cid = str(item.get("chat_id") or "").strip()
                    if sid and cid:
                        self.remember_chat(sid, cid)
            except Exception as exc:
                logger.debug("FeishuPushForwarder wake watch load failed: %s", exc)
            feishu_client = FeishuClient(timeout_seconds=self._timeout)
            recovery_results = await client.poll_stream_recoveries()
            for pr in recovery_results or []:
                meta = pr.get("metadata")
                chat_id = str(
                    meta.get("feishu_chat_id") if isinstance(meta, dict) else ""
                ).strip()
                if not chat_id:
                    continue
                sent = await self._send_result_payload(
                    feishu_client=feishu_client,
                    chat_id=chat_id,
                    payload=pr,
                )
                if sent:
                    session_id = str(pr.get("session_id") or "").strip()
                    if session_id:
                        self.remember_chat(session_id, chat_id)
            dispatched: list[Dict[str, Any]] = []
            poll_frontend = getattr(client, "poll_frontend_push", None)
            if poll_frontend is not None:
                try:
                    dispatched = list(await poll_frontend() or [])
                except Exception as exc:
                    logger.warning(
                        "FeishuPushForwarder poll_frontend_push failed: %s", exc
                    )
            acked: Dict[str, list[str]] = {}
            failed: Dict[str, list[str]] = {}
            unrouted: Dict[str, list[str]] = {}
            for pr in dispatched:
                if not isinstance(pr, dict):
                    continue
                session_id = str(pr.get("session_id") or "").strip()
                request_id = str(pr.get("request_id") or "").strip()
                chat_id = self.chat_id_for_session(session_id)
                if not chat_id:
                    meta = pr.get("metadata")
                    if isinstance(meta, dict):
                        chat_id = str(meta.get("feishu_chat_id") or "").strip()
                    if chat_id and session_id:
                        self.remember_chat(session_id, chat_id)
                if not session_id or not chat_id or not request_id:
                    if session_id and request_id:
                        unrouted.setdefault(session_id, []).append(request_id)
                    continue
                sent = await self._send_result_payload(
                    feishu_client=feishu_client,
                    chat_id=chat_id,
                    payload=pr,
                )
                if sent or not _push_payload_needs_delivery(pr):
                    acked.setdefault(session_id, []).append(request_id)
                else:
                    failed.setdefault(session_id, []).append(request_id)
            await self._settle_push(client, "ack_push", acked)
            await self._settle_push(client, "release_push", failed)
            await self._settle_push(
                client, "release_push", unrouted, count_attempt=False
            )
        except Exception as exc:
            logger.debug("FeishuPushForwarder _poll_once: %s", exc)

    async def _settle_push(
        self,
        client: AutomationIPCClient,
        method_name: str,
        buckets: Dict[str, list[str]],
        **kwargs: Any,
    ) -> None:
        fn = getattr(client, method_name, None)
        if fn is None or not buckets:
            return
        for session_id, request_ids in buckets.items():
            if not request_ids:
                continue
            try:
                await fn(session_id, request_ids, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "FeishuPushForwarder %s session=%s failed: %s",
                    method_name,
                    session_id,
                    exc,
                )

    async def _send_result_payload(
        self,
        *,
        feishu_client: FeishuClient,
        chat_id: str,
        payload: Dict[str, Any],
    ) -> bool:
        from agent_core.agent.media_helpers import filter_undelivered_attachments

        meta = payload.get("metadata")
        skip_final_reply = isinstance(meta, dict) and bool(
            meta.get("feishu_skip_final_reply")
        )
        text = (payload.get("output_text") or "").strip()
        # 后台轮次（定时任务/子任务推送）失败时也要让用户看到原因，而不是只推一句内部错误串
        err_raw = meta.get("_error_info") if isinstance(meta, dict) else None
        if err_raw:
            try:
                from system.kernel.error_notify import (
                    build_user_facing_error_card,
                    error_info_from_dict,
                )

                await feishu_client.send_interactive_card(
                    chat_id=chat_id,
                    card=build_user_facing_error_card(error_info_from_dict(err_raw)),
                )
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("FeishuPushForwarder 错误卡片发送失败: %s", exc)
        attachments = payload.get("attachments")
        if not isinstance(attachments, list):
            attachments = []
        attachments = filter_undelivered_attachments(attachments)
        if skip_final_reply and not attachments:
            return True
        if not text and not attachments:
            return False
        try:
            # 与 send_message 主路径一致：按 reply_format 发 Markdown 卡片或纯文本，
            # 避免 poll_push 走 send_text_message 的 Markdown→纯文本过滤。
            if text and not skip_final_reply:
                await send_feishu_agent_final_reply(
                    client=feishu_client,
                    chat_id=chat_id,
                    output_text=text,
                )
            if attachments:
                await feishu_client.send_reply_attachments(
                    chat_id=chat_id,
                    attachments=attachments,
                )
            return True
        except Exception as exc:
            logger.warning(
                "FeishuPushForwarder send to chat_id=%s failed: %s",
                chat_id,
                exc,
            )
            return False
