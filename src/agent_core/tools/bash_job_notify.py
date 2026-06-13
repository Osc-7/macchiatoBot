"""
后台 bash 任务完成通知注册表。

目标：
- 在 bash 启动后台任务时登记（本地 / 远程）；
- 由 daemon 后台 watcher 主动轮询终态；
- 通过 KernelScheduler.inject_turn 主动唤醒父会话；
- 父会话有 inflight 请求时暂存，安全点 flush；
- 终态仅通知一次，避免重复噪声。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from system.kernel.core_pool import CorePool
    from system.kernel.scheduler import KernelScheduler

_TERMINAL_STATUSES = frozenset({"finished", "failed", "timed_out", "cancelled"})

logger = logging.getLogger(__name__)


@dataclass
class _TrackedJob:
    session_id: str
    job_id: str
    command: str
    cwd: str
    log_path: str
    created_at: float
    remote: bool
    workspace_root: Optional[str] = None
    remote_login: Optional[str] = None
    # 用于 inject_turn 的元数据
    frontend_id: str = "cli"
    source: str = "cli"
    user_id: str = "root"
    metadata: Dict[str, Any] = field(default_factory=dict)
    # 终态已通知后设为 True，并从注册表移除
    notified: bool = False
    # 已暂存待 flush（父会话 inflight>0），避免同一 job 被重复 poll/暂存
    staged: bool = False


_LOCK = Lock()
_TRACKED_BY_SESSION: Dict[str, Dict[str, _TrackedJob]] = {}
# session_id -> 待注入通知条目列表（父会话 inflight>0 时暂存）
# 每个条目为 {"text": str, "note": Optional[Dict]}，flush 时需保留 note 以标记已通知
_PENDING_BY_SESSION: Dict[str, List[Dict[str, Any]]] = {}

# daemon 启动时注入的依赖（避免模块级 import scheduler/core_pool 单例）
_notify_dependencies: Dict[str, Any] = {}


def _job_key(job_id: str, *, remote: bool) -> str:
    return f"{'r' if remote else 'l'}:{job_id}"


def _now() -> float:
    return time.monotonic()


def set_notify_dependencies(
    scheduler: Optional["KernelScheduler"] = None,
    core_pool: Optional["CorePool"] = None,
) -> None:
    """由 daemon 启动时注入 scheduler / core_pool 依赖。"""
    _notify_dependencies["scheduler"] = scheduler
    _notify_dependencies["core_pool"] = core_pool


def get_notify_dependencies() -> Dict[str, Any]:
    return dict(_notify_dependencies)


def register_local_job(
    *,
    session_id: str,
    job_id: str,
    command: str,
    cwd: str,
    log_path: str,
    workspace_root: str,
    frontend_id: str = "cli",
    source: str = "cli",
    user_id: str = "root",
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    sid = str(session_id or "").strip()
    jid = str(job_id or "").strip()
    if not sid or not jid:
        return
    rec = _TrackedJob(
        session_id=sid,
        job_id=jid,
        command=str(command or ""),
        cwd=str(cwd or ""),
        log_path=str(log_path or ""),
        workspace_root=str(workspace_root or ""),
        remote=False,
        created_at=time.time(),
        frontend_id=str(frontend_id or "cli").strip() or "cli",
        source=str(source or "cli").strip() or "cli",
        user_id=str(user_id or "root").strip() or "root",
        metadata=dict(metadata or {}),
        notified=False,
    )
    with _LOCK:
        jobs = _TRACKED_BY_SESSION.setdefault(sid, {})
        jobs[_job_key(jid, remote=False)] = rec


def register_remote_job(
    *,
    session_id: str,
    remote_login: str,
    job_id: str,
    command: str,
    cwd: str,
    log_path: str,
    frontend_id: str = "cli",
    source: str = "cli",
    user_id: str = "root",
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    sid = str(session_id or "").strip()
    jid = str(job_id or "").strip()
    login = str(remote_login or "").strip()
    if not sid or not jid or not login:
        return
    rec = _TrackedJob(
        session_id=sid,
        job_id=jid,
        command=str(command or ""),
        cwd=str(cwd or ""),
        log_path=str(log_path or ""),
        remote=True,
        remote_login=login,
        created_at=time.time(),
        frontend_id=str(frontend_id or "cli").strip() or "cli",
        source=str(source or "cli").strip() or "cli",
        user_id=str(user_id or "root").strip() or "root",
        metadata=dict(metadata or {}),
        notified=False,
    )
    with _LOCK:
        jobs = _TRACKED_BY_SESSION.setdefault(sid, {})
        jobs[_job_key(jid, remote=True)] = rec


async def poll_terminal_jobs(*, max_items: int = 20) -> List[Dict[str, Any]]:
    """跨所有 session 轮询终态 job，返回通知负载（不删除注册表记录）。"""
    if max_items <= 0:
        return []
    with _LOCK:
        sessions = list(_TRACKED_BY_SESSION.items())

    out: List[Dict[str, Any]] = []
    for sid, jobs in sessions:
        for key, rec in list(jobs.items()):
            if len(out) >= max_items:
                break
            if rec.notified or rec.staged:
                continue
            status_payload = await _query_job_status(rec)
            if status_payload is None:
                continue
            status = str(status_payload.get("status") or "").strip().lower()
            if status not in _TERMINAL_STATUSES:
                continue
            out.append(
                {
                    "session_id": rec.session_id,
                    "job_id": rec.job_id,
                    "status": status,
                    "exit_code": status_payload.get("exit_code"),
                    "duration_seconds": status_payload.get("duration_seconds"),
                    "timed_out": bool(status_payload.get("timed_out", False)),
                    "command": rec.command,
                    "cwd": rec.cwd,
                    "log_path": rec.log_path
                    or str(status_payload.get("log_path") or ""),
                    "remote": rec.remote,
                    "remote_login": rec.remote_login,
                    "frontend_id": rec.frontend_id,
                    "source": rec.source,
                    "user_id": rec.user_id,
                    "metadata": dict(rec.metadata),
                }
            )
    return out


async def _query_job_status(rec: _TrackedJob) -> Optional[Dict[str, Any]]:
    try:
        if rec.remote:
            from agent_core.remote.worker_registry import get_remote_worker_registry

            registry = get_remote_worker_registry()
            res = await registry.job_status(
                login=str(rec.remote_login or ""),
                session_id=rec.session_id,
                job_id=rec.job_id,
            )
            if str(getattr(res, "error", "") or "") == "JOB_NOT_FOUND":
                return {
                    "status": "failed",
                    "exit_code": None,
                    "timed_out": False,
                    "duration_seconds": 0.0,
                    "log_path": rec.log_path,
                }
            return {
                "status": str(getattr(res, "status", "") or ""),
                "exit_code": getattr(res, "exit_code", None),
                "timed_out": bool(getattr(res, "timed_out", False)),
                "duration_seconds": float(getattr(res, "duration_seconds", 0.0) or 0.0),
                "log_path": str(getattr(res, "log_path", "") or rec.log_path),
            }

        from agent_core.job_manager import get_job_manager

        mgr = get_job_manager(workspace_root=rec.workspace_root or ".")
        handle = await mgr.job_status(rec.job_id)
        if handle is None:
            return {
                "status": "failed",
                "exit_code": None,
                "timed_out": False,
                "duration_seconds": 0.0,
                "log_path": rec.log_path,
            }
        return {
            "status": str(getattr(handle, "status", "") or ""),
            "exit_code": getattr(handle, "exit_code", None),
            "timed_out": bool(getattr(handle, "timed_out", False)),
            "duration_seconds": float(getattr(handle, "duration_seconds", 0.0) or 0.0),
            "log_path": str(getattr(handle, "log_path", rec.log_path)),
        }
    except Exception:
        # 轮询异常时保持静默，留待后续轮次继续尝试。
        return None


def format_notification(note: Dict[str, Any]) -> str:
    """统一后台任务完成通知文案。"""
    job_id = str(note.get("job_id") or "")
    status = str(note.get("status") or "unknown")
    remote = bool(note.get("remote", False))
    login = str(note.get("remote_login") or "").strip()
    exit_code = note.get("exit_code")
    duration = note.get("duration_seconds")
    timed_out = bool(note.get("timed_out", False))

    scope = "远程" if remote else "本地"
    if remote and login:
        scope = f"{scope}({login})"

    parts: List[str] = []
    if exit_code is not None:
        parts.append(f"exit={exit_code}")
    if isinstance(duration, (int, float)):
        parts.append(f"{float(duration):.1f}s")
    if timed_out:
        parts.append("timed_out=true")
    suffix = f"（{', '.join(parts)}）" if parts else ""

    return (
        f"[后台任务完成] {scope}任务 {job_id} 已结束：{status}{suffix}。\n"
        "请用 bash job_tail 查看日志；无需反复 job_status 轮询。"
    )


def stage_notification(
    session_id: str, text: str, note: Optional[Dict[str, Any]] = None
) -> None:
    """父会话 inflight>0 时，将通知正文暂存到 session 级队列。

    如果提供了 note，会将对应 job 标记为 staged，避免同一 job 在 flush 前被重复 poll/暂存。
    """
    sid = str(session_id or "").strip()
    if not sid or not text:
        return
    with _LOCK:
        if note is not None:
            jid = str(note.get("job_id") or "").strip()
            if jid:
                jobs = _TRACKED_BY_SESSION.get(sid, {})
                key = _job_key(jid, remote=bool(note.get("remote", False)))
                rec = jobs.get(key)
                if rec is not None:
                    rec.staged = True
        _PENDING_BY_SESSION.setdefault(sid, []).append({"text": text, "note": note})


def flush_pending_for_session(session_id: str) -> None:
    """在父会话 _run_and_route finally（inflight==0）时 flush 暂存通知。"""
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _LOCK:
        pending = _PENDING_BY_SESSION.pop(sid, [])
    if not pending:
        return
    deps = get_notify_dependencies()
    scheduler = deps.get("scheduler")
    core_pool = deps.get("core_pool")
    for item in pending:
        text = item.get("text", "")
        note = item.get("note")
        try:
            deliver_via_inject(
                session_id=sid,
                text=text,
                scheduler=scheduler,
                core_pool=core_pool,
                note=note,
            )
        except Exception as exc:
            logger.warning(
                "bash_job_notify: flush deliver failed session=%s: %s", sid, exc
            )
            if note is not None:
                _reset_staged(
                    sid,
                    str(note.get("job_id") or ""),
                    remote=bool(note.get("remote", False)),
                )


def build_feishu_inject_metadata(
    session_id: str,
    core_pool: Optional["CorePool"],
) -> Optional[Dict[str, Any]]:
    """为飞书会话构建 inject_turn 所需的 hooks 元数据；非飞书返回 None。"""
    if not session_id.startswith("feishu:"):
        return None
    try:
        from frontend.feishu.feishu_turn_hooks import (
            FeishuTurnHooksController,
            resolve_feishu_chat_id_for_session,
        )

        chat_id = resolve_feishu_chat_id_for_session(session_id, core_pool=core_pool)
        if not chat_id:
            return None
        ctrl = FeishuTurnHooksController(
            chat_id=chat_id,
            markdown_header_title="后台任务通知",
        )
        return {
            "_hooks": ctrl.hooks,
            "_feishu_hook_ctx": ctrl,
            "feishu_chat_id": chat_id,
        }
    except Exception as exc:
        logger.warning(
            "bash_job_notify: feishu inject hooks skipped session=%s: %s",
            session_id,
            exc,
        )
        return None


def deliver_via_inject(
    session_id: str,
    text: str,
    *,
    scheduler: Optional["KernelScheduler"] = None,
    core_pool: Optional["CorePool"] = None,
    note: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    将后台任务完成通知通过 KernelScheduler.inject_turn 主动投递。

    - session 当前有 inflight kernel 请求时：暂存，稍后 flush。
    - 否则直接 inject_turn，触发完整一轮 Agent 回复。
    """
    sid = str(session_id or "").strip()
    if not sid or not text:
        return False

    # 如果调用方没传依赖，使用 daemon 注入的全局依赖
    if scheduler is None:
        scheduler = get_notify_dependencies().get("scheduler")
    if core_pool is None:
        core_pool = get_notify_dependencies().get("core_pool")

    if scheduler is None:
        logger.warning(
            "bash_job_notify: scheduler not set, cannot inject session=%s job=%s",
            sid,
            note.get("job_id") if note else "?",
        )
        return False

    cnt_fn = getattr(scheduler, "session_inflight_request_count", None)
    inflight = 0
    if callable(cnt_fn):
        try:
            inflight = int(cnt_fn(sid))
        except (TypeError, ValueError):
            inflight = 0

    if inflight > 0:
        jid = str(note.get("job_id") or "").strip() if note else ""
        if jid and _is_staged(sid, jid, remote=bool(note.get("remote", False))):
            logger.info(
                "bash_job_notify: already-staged session=%s job=%s status=%s inflight=%d",
                sid,
                jid,
                note.get("status") if note else "?",
                inflight,
            )
            return True
        stage_notification(sid, text, note=note)
        logger.info(
            "bash_job_notify: staged session=%s job=%s status=%s inflight=%d",
            sid,
            note.get("job_id") if note else "?",
            note.get("status") if note else "?",
            inflight,
        )
        return True

    from agent_core.kernel_interface.action import KernelRequest

    inject_md: Dict[str, Any] = {
        "source": note.get("source") if note else "cli",
        "user_id": note.get("user_id") if note else "root",
    }
    feishu_md = build_feishu_inject_metadata(sid, core_pool)
    if feishu_md:
        inject_md.update(feishu_md)

    request = KernelRequest.create(
        text=text,
        session_id=sid,
        frontend_id="bash_job",
        priority=-1,
        metadata=inject_md,
    )
    logger.info(
        "bash_job_notify: delivered session=%s job=%s status=%s staged=false",
        sid,
        note.get("job_id") if note else "?",
        note.get("status") if note else "?",
    )
    try:
        scheduler.inject_turn(request)
    except Exception as exc:
        logger.warning("bash_job_notify: inject_turn failed session=%s: %s", sid, exc)
        return False

    # 标记该 job 已通知并从注册表移除
    if note is not None:
        _mark_notified(
            sid,
            str(note.get("job_id") or ""),
            remote=bool(note.get("remote", False)),
        )
    return True


def _mark_notified(session_id: str, job_id: str, *, remote: bool) -> None:
    with _LOCK:
        jobs = _TRACKED_BY_SESSION.get(session_id, {})
        key = _job_key(job_id, remote=remote)
        rec = jobs.get(key)
        if rec is None:
            return
        rec.notified = True
        jobs.pop(key, None)
        if not jobs:
            _TRACKED_BY_SESSION.pop(session_id, None)


def _reset_staged(session_id: str, job_id: str, *, remote: bool) -> None:
    """flush 失败时重置 staged 标记，允许后续 poll 重新尝试投递。"""
    with _LOCK:
        jobs = _TRACKED_BY_SESSION.get(session_id, {})
        key = _job_key(job_id, remote=remote)
        rec = jobs.get(key)
        if rec is None:
            return
        rec.staged = False


def _is_staged(session_id: str, job_id: str, *, remote: bool) -> bool:
    with _LOCK:
        jobs = _TRACKED_BY_SESSION.get(session_id, {})
        key = _job_key(job_id, remote=remote)
        rec = jobs.get(key)
        return rec is not None and rec.staged


# ── 旧版兼容 API（保留供现有测试/调用方过渡）───────────────────────────


async def poll_completed_notifications(
    *,
    session_id: str,
    max_items: int = 3,
) -> List[Dict[str, Any]]:
    """轮询指定 session 的后台任务终态通知（每个 job 只返回一次）。"""
    sid = str(session_id or "").strip()
    if not sid or max_items <= 0:
        return []
    notes = await poll_terminal_jobs(max_items=max_items)
    result = [n for n in notes if n.get("session_id") == sid]
    # 旧语义：返回后标记为已通知/移除
    for note in result:
        _mark_notified(
            sid, str(note.get("job_id") or ""), remote=bool(note.get("remote", False))
        )
    return result


def clear_session_tracking_for_tests(session_id: str) -> None:
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _LOCK:
        _TRACKED_BY_SESSION.pop(sid, None)
        _PENDING_BY_SESSION.pop(sid, None)


def clear_all_tracking_for_tests() -> None:
    with _LOCK:
        _TRACKED_BY_SESSION.clear()
        _PENDING_BY_SESSION.clear()
