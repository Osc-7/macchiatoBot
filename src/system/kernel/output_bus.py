"""
OutputBus — 输出消息总线。

从 scheduler.py 拆分出来以简化调度器文件体积，保持职责清晰：
- OutputBus 负责 request_id 级别等待 + session 级别广播
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from typing import Any, Callable, Dict, Optional

from agent_core.interfaces import AgentRunResult

logger = logging.getLogger(__name__)


class OutputBus:
    """
    输出消息总线。

    提供两类能力：
    1) request_id 级别等待：submit 后可 await 对应结果
    2) session 级别广播：结果可广播给订阅者（供推送前端使用）
    """

    def __init__(self) -> None:
        self._pending: Dict[str, asyncio.Future[AgentRunResult]] = {}
        self._listeners: Dict[str, Dict[str, Callable[[str, AgentRunResult], Any]]] = defaultdict(dict)

    def register_waiter(self, request_id: str) -> None:
        """注册一个 request_id 的等待位。"""
        loop = asyncio.get_running_loop()
        self._pending[request_id] = loop.create_future()

    async def wait_result(
        self, request_id: str, timeout_seconds: Optional[float] = None
    ) -> AgentRunResult:
        """等待 request_id 对应结果。"""
        fut = self._pending.get(request_id)
        if fut is None:
            raise RuntimeError(f"request_id not registered: {request_id}")
        if timeout_seconds is None:
            result = await fut
        else:
            result = await asyncio.wait_for(fut, timeout=float(timeout_seconds))
        return result

    def subscribe(
        self, session_id: str, callback: Callable[[str, AgentRunResult], Any]
    ) -> str:
        """订阅某个 session 的输出广播，返回 subscription_id。"""
        sub_id = str(uuid.uuid4())[:12]
        self._listeners[session_id][sub_id] = callback
        return sub_id

    def unsubscribe(self, session_id: str, subscription_id: str) -> None:
        """取消订阅。"""
        listeners = self._listeners.get(session_id)
        if not listeners:
            return
        listeners.pop(subscription_id, None)
        if not listeners:
            self._listeners.pop(session_id, None)

    async def publish(self, session_id: str, request_id: str, result: AgentRunResult) -> None:
        """发布一个结果到总线：先解锁等待者，再广播给订阅者。"""
        fut = self._pending.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(result)
        listeners = list(self._listeners.get(session_id, {}).values())
        for cb in listeners:
            try:
                maybe = cb(request_id, result)
                if asyncio.iscoroutine(maybe):
                    asyncio.create_task(maybe)
            except Exception as exc:
                logger.warning("OutputBus: listener callback failed: %s", exc)
                continue

    async def publish_error(self, request_id: str, exc: BaseException) -> None:
        """向 request_id 对应等待者发布错误。"""
        fut = self._pending.pop(request_id, None)
        if fut is None:
            return
        if not fut.done():
            fut.set_exception(exc)

    def cancel_all(self) -> None:
        """关闭时取消所有挂起等待。"""
        for request_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    def has_waiter(self, request_id: str) -> bool:
        """判断指定 request_id 是否有对应的等待者。"""
        return request_id in self._pending

    @property
    def pending_count(self) -> int:
        return len(self._pending)

