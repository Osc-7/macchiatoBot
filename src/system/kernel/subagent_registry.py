"""
SubagentRegistry — 子 Agent 生命周期注册表。

维护 parent session ↔ subagent 的映射关系，以及每个 subagent 对应的
asyncio.Task 引用（用于取消）。

当 subagent 完成/失败时，通过 KernelScheduler.inject_turn() 立即唤醒父 session，
实现 first-done 语义（多个并行子任务中先完成的先唤醒，无需 barrier）。

循环依赖处理：SubagentRegistry 构造时不持有 scheduler，
由 daemon 初始化末尾通过 set_scheduler() 注入，避免：
  SubagentRegistry → KernelScheduler → CorePool → build_tool_registry
  → SubagentTools → SubagentRegistry 的循环。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Literal, Optional

if TYPE_CHECKING:
    from .scheduler import KernelScheduler

logger = logging.getLogger(__name__)


@dataclass
class SubagentInfo:
    """单个 subagent 的进程控制块。"""

    subagent_id: str
    parent_session_id: str
    task_description: str
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
    result: Optional[str] = None
    error: Optional[str] = None
    bg_task: Optional[asyncio.Task] = None   # asyncio.Task 引用，用于 cancel()
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None


class SubagentRegistry:
    """
    Subagent 生命周期注册表。

    Usage（in daemon）::

        registry = SubagentRegistry()
        core_pool = CorePool(..., subagent_registry=registry)
        scheduler = KernelScheduler(kernel=kernel, core_pool=core_pool)
        registry.set_scheduler(scheduler)   # 后绑定，避免循环依赖

    Usage（in tools）::

        subagent_id = str(uuid.uuid4())
        info = SubagentInfo(
            subagent_id=subagent_id,
            parent_session_id=parent_session,
            task_description=task,
        )
        registry.register(info)
        bg = asyncio.create_task(_run_subagent_task(...))
        info.bg_task = bg
    """

    def __init__(self) -> None:
        self._registry: Dict[str, SubagentInfo] = {}
        self._scheduler: Optional["KernelScheduler"] = None

    def set_scheduler(self, scheduler: "KernelScheduler") -> None:
        """后绑定 KernelScheduler（daemon 初始化末尾调用）。"""
        self._scheduler = scheduler

    def register(self, info: SubagentInfo) -> None:
        """注册一个新的 subagent。"""
        self._registry[info.subagent_id] = info
        desc = info.task_description or ""
        task_preview = desc[:80].replace("\n", " ")
        if len(desc) > 80:
            task_preview += "..."
        logger.info(
            "SubagentRegistry: registered subagent_id=%s parent_session_id=%s task_preview=%s",
            info.subagent_id,
            info.parent_session_id,
            task_preview,
            extra={"subagent_id": info.subagent_id, "parent_session_id": info.parent_session_id},
        )

    def get(self, subagent_id: str) -> Optional[SubagentInfo]:
        """根据 subagent_id 获取 SubagentInfo。"""
        return self._registry.get(subagent_id)

    def list_by_parent(self, parent_session_id: str) -> List[SubagentInfo]:
        """列出指定父 session 的所有 subagent。"""
        return [
            info
            for info in self._registry.values()
            if info.parent_session_id == parent_session_id
        ]

    def on_complete(self, subagent_id: str, result: str) -> None:
        """
        Subagent 成功完成时调用。

        更新状态为 completed，并通过 inject_turn() 立即唤醒父 session，
        实现 first-done 语义。
        """
        info = self._registry.get(subagent_id)
        if info is None:
            logger.warning(
                "SubagentRegistry.on_complete: unknown subagent_id=%s", subagent_id
            )
            return

        info.status = "completed"
        info.result = result
        info.completed_at = time.time()
        duration_sec = (info.completed_at - info.created_at) if info.created_at else None

        logger.info(
            "SubagentRegistry: subagent completed subagent_id=%s parent_session_id=%s result_len=%s duration_sec=%s",
            subagent_id,
            info.parent_session_id,
            len(result),
            round(duration_sec, 2) if duration_sec is not None else None,
            extra={"subagent_id": subagent_id, "parent_session_id": info.parent_session_id, "status": "completed"},
        )

        # 只推送完成通知 + 结果预览，完整结果由父 Agent 按需拉取（notify-and-pull）
        task_preview = (info.task_description or "")[:80]
        result_preview = (result or "")[:200]
        ellipsis = "..." if len(result or "") > 200 else ""
        notification = (
            f"[子任务 {subagent_id} 完成]\n"
            f"任务：{task_preview}\n"
            f"结果预览：{result_preview}{ellipsis}\n\n"
            f"如需完整结果，调用 get_subagent_status(subagent_id=\"{subagent_id}\", include_full_result=True)"
        )
        self._inject_to_parent(info, notification)

    def on_fail(self, subagent_id: str, error: str) -> None:
        """
        Subagent 失败时调用。

        更新状态为 failed，并通过 inject_turn() 通知父 session（也是 first-done）。
        """
        info = self._registry.get(subagent_id)
        if info is None:
            logger.warning(
                "SubagentRegistry.on_fail: unknown subagent_id=%s", subagent_id
            )
            return

        info.status = "failed"
        info.error = error
        info.completed_at = time.time()
        duration_sec = (info.completed_at - info.created_at) if info.created_at else None
        error_preview = (error or "")[:200].replace("\n", " ")

        logger.info(
            "SubagentRegistry: subagent failed subagent_id=%s parent_session_id=%s duration_sec=%s error_preview=%s",
            subagent_id,
            info.parent_session_id,
            round(duration_sec, 2) if duration_sec is not None else None,
            error_preview + ("..." if len(error or "") > 200 else ""),
            extra={"subagent_id": subagent_id, "parent_session_id": info.parent_session_id, "status": "failed"},
        )
        logger.debug(
            "SubagentRegistry: subagent full error subagent_id=%s error=%s",
            subagent_id,
            error,
        )

        fail_text = f"[子任务 {subagent_id} 失败]\n错误：{error}"
        self._inject_to_parent(info, fail_text)

    def cancel(self, subagent_id: str) -> bool:
        """
        取消指定 subagent。

        取消后台 asyncio.Task，更新状态为 cancelled。
        返回 True 表示成功取消（或已完成/取消），False 表示不存在。
        """
        info = self._registry.get(subagent_id)
        if info is None:
            logger.warning(
                "SubagentRegistry.cancel: unknown subagent_id=%s", subagent_id
            )
            return False

        if info.status in ("completed", "failed", "cancelled"):
            logger.info(
                "SubagentRegistry: cancel no-op subagent_id=%s already status=%s",
                subagent_id,
                info.status,
                extra={"subagent_id": subagent_id, "parent_session_id": info.parent_session_id},
            )
            return True

        previous_status = info.status
        if info.bg_task is not None and not info.bg_task.done():
            info.bg_task.cancel()
            logger.info(
                "SubagentRegistry: cancelled bg_task subagent_id=%s parent_session_id=%s previous_status=%s",
                subagent_id,
                info.parent_session_id,
                previous_status,
                extra={"subagent_id": subagent_id, "parent_session_id": info.parent_session_id, "status": "cancelled"},
            )
        else:
            logger.info(
                "SubagentRegistry: marked cancelled (no bg_task or already done) subagent_id=%s parent_session_id=%s",
                subagent_id,
                info.parent_session_id,
                extra={"subagent_id": subagent_id, "parent_session_id": info.parent_session_id, "status": "cancelled"},
            )

        info.status = "cancelled"
        info.completed_at = time.time()
        return True

    def remove(self, subagent_id: str) -> None:
        """从注册表中移除 subagent（资源清理后调用）。"""
        self._registry.pop(subagent_id, None)

    def _inject_to_parent(self, info: SubagentInfo, content: str) -> None:
        """向父 session 注入一条 inject_turn 消息。"""
        from agent_core.kernel_interface.action import AgentMessage, KernelRequest

        if self._scheduler is None:
            logger.warning(
                "SubagentRegistry: scheduler not set, cannot inject_turn subagent_id=%s parent_session_id=%s",
                info.subagent_id,
                info.parent_session_id,
                extra={"subagent_id": info.subagent_id, "parent_session_id": info.parent_session_id},
            )
            return

        if not info.parent_session_id:
            logger.error(
                "SubagentRegistry: parent_session_id is empty for subagent_id=%s — "
                "inject_turn aborted to prevent ValueError: session_id 不能为空",
                info.subagent_id,
                extra={"subagent_id": info.subagent_id},
            )
            return

        msg_type = "task" if info.status == "completed" else "notify"
        message_id = str(uuid.uuid4())
        agent_msg = AgentMessage(
            message_id=message_id,
            sender_session=f"sub:{info.subagent_id}",
            receiver_session=info.parent_session_id,
            message_type=msg_type,
            subagent_id=info.subagent_id,
        )

        # 构造注入请求：priority=-1（高优先级），跳过 Future 注册
        request = KernelRequest.create(
            text=content,
            session_id=info.parent_session_id,
            frontend_id="subagent",
            priority=-1,
            metadata={"_agent_message": agent_msg},
        )

        logger.info(
            "SubagentRegistry: inject_to_parent message_id=%s subagent_id=%s parent_session_id=%s message_type=%s content_len=%s",
            message_id[:8],
            info.subagent_id,
            info.parent_session_id,
            msg_type,
            len(content),
            extra={
                "subagent_id": info.subagent_id,
                "parent_session_id": info.parent_session_id,
                "message_id": message_id,
                "message_type": msg_type,
            },
        )
        try:
            self._scheduler.inject_turn(request)
        except Exception as exc:
            logger.warning(
                "SubagentRegistry: inject_turn failed subagent_id=%s parent_session_id=%s error=%s",
                info.subagent_id,
                info.parent_session_id,
                exc,
                extra={"subagent_id": info.subagent_id, "parent_session_id": info.parent_session_id},
            )
