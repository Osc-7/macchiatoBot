"""request_permission：pending Future 与前端通知钩子。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_notify_hook: Optional[Callable[[str, Dict[str, Any]], None]] = None

_permission_ipc_stream_notify: ContextVar[
    Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]]
] = ContextVar("permission_ipc_stream_notify", default=None)


@asynccontextmanager
async def permission_ipc_stream_notify_scope(
    forward: Callable[[str, Dict[str, Any]], Awaitable[None]],
):
    """仅在 automation_daemon 处理 ``run_turn_stream`` → ``inject_message`` 时挂接。"""
    token = _permission_ipc_stream_notify.set(forward)
    try:
        yield
    finally:
        _permission_ipc_stream_notify.reset(token)


def set_permission_notify_hook(
    fn: Optional[Callable[[str, Dict[str, Any]], None]],
) -> None:
    """由前端/connector 注册：收到 (permission_id, payload) 时推送到人类。"""
    global _notify_hook
    _notify_hook = fn


def get_permission_notify_hook() -> Optional[Callable[[str, Dict[str, Any]], None]]:
    return _notify_hook


@dataclass
class PermissionDecision:
    """人类或系统对权限请求的裁决。"""

    allowed: bool
    """是否允许（一次性或已追加前缀）。"""
    path_prefix: Optional[str] = None
    """若允许持久写某前缀，规范化绝对路径；仅当 allowed 为 True 时有效。"""
    note: Optional[str] = None
    clarify_requested: bool = False
    """用户暂不批准，希望先补充更精确说明后再决定（飞书卡片第三项）。"""
    user_instruction: Optional[str] = None
    """飞书表单「给 Agent 更精确的指令」中用户填写的文本（未填则为空字符串）。"""
    persist_acl: bool = False
    """path_prefix 存在时：人类是否选择「加入持久白名单」。
    True=写入 writable_roots；False=仅进程内临时放行。
    **须由人类在前端（如飞书卡片「本次有效 / 加白名单」）决定；Agent/模型不得自行设定此字段。**"""


_futures: Dict[str, asyncio.Future[PermissionDecision]] = {}


def register_permission_wait() -> tuple[str, asyncio.Future[PermissionDecision]]:
    """创建等待中的 permission_id 与 Future（由 request_permission 工具 await）。"""
    pid = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[PermissionDecision] = loop.create_future()
    _futures[pid] = fut
    return pid, fut


def resolve_permission(permission_id: str, decision: PermissionDecision) -> bool:
    """由**人类操作前端**（或单测）在用户裁决后调用，唤醒挂起的工具。

    ``persist_acl`` 仅应由飞书卡片等 UI 根据用户点击写入；勿由 Agent 逻辑伪造。"""
    pid = (permission_id or "").strip()
    fut = _futures.pop(pid, None)
    if fut is None:
        logger.warning("resolve_permission: unknown or already resolved id=%s", pid)
        return False
    if fut.done():
        return False
    fut.set_result(decision)
    return True


def cancel_permission_wait(permission_id: str, *, reason: str = "cancelled") -> bool:
    """取消等待（例如 Core 关闭）。"""
    fut = _futures.pop(permission_id, None)
    if fut is None or fut.done():
        return False
    fut.set_exception(asyncio.CancelledError(reason))
    return True


def notify_permission_pending(permission_id: str, payload: Dict[str, Any]) -> None:
    stream_fn = _permission_ipc_stream_notify.get()
    if stream_fn is not None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("permission notify: ipc stream 需要运行中事件循环")
            return
        loop.create_task(stream_fn(permission_id, payload))
        return
    if _notify_hook is not None:
        try:
            _notify_hook(permission_id, payload)
        except Exception as exc:
            logger.warning("permission notify hook failed: %s", exc)
