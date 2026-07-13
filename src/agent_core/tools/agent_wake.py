"""
Agent 会话定时唤醒注册表。

与 create_scheduled_job（独立 cron 会话）不同，本模块在指定时间向**当前会话**
通过 KernelScheduler.inject_turn 主动唤醒，复用 bash_job_notify 的投递语义。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from system.kernel.core_pool import CorePool
    from system.kernel.scheduler import KernelScheduler

logger = logging.getLogger(__name__)

_LOCK = Lock()
_WAKES: Dict[str, "_ScheduledWake"] = {}
# session_id -> 待注入唤醒正文（父会话 inflight>0 时暂存）
_PENDING_BY_SESSION: Dict[str, List[Dict[str, Any]]] = {}


@dataclass
class _ScheduledWake:
    wake_id: str
    session_id: str
    fire_at: float
    message: str
    label: str = ""
    created_at: float = field(default_factory=time.time)
    frontend_id: str = "cli"
    source: str = "cli"
    user_id: str = "root"
    metadata: Dict[str, Any] = field(default_factory=dict)
    fired: bool = False
    staged: bool = False


def _now() -> float:
    return time.time()


def _new_wake_id() -> str:
    return f"wake-{uuid.uuid4().hex[:12]}"


def register_wake(
    *,
    session_id: str,
    fire_at: float,
    message: str,
    wake_id: Optional[str] = None,
    label: str = "",
    frontend_id: str = "cli",
    source: str = "cli",
    user_id: str = "root",
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """登记一条待触发的会话唤醒，返回 wake_id。"""
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    text = str(message or "").strip()
    if not text:
        raise ValueError("message is required")
    wid = str(wake_id or "").strip() or _new_wake_id()
    rec = _ScheduledWake(
        wake_id=wid,
        session_id=sid,
        fire_at=float(fire_at),
        message=text,
        label=str(label or "").strip(),
        created_at=_now(),
        frontend_id=str(frontend_id or "cli").strip() or "cli",
        source=str(source or "cli").strip() or "cli",
        user_id=str(user_id or "root").strip() or "root",
        metadata=dict(metadata or {}),
    )
    with _LOCK:
        _WAKES[wid] = rec
    _sync_feishu_push_watch_file()
    logger.info(
        "agent_wake: registered wake_id=%s session=%s fire_at=%.0f label=%r",
        wid,
        sid,
        rec.fire_at,
        rec.label or "",
    )
    return wid


def cancel_wake(wake_id: str) -> bool:
    wid = str(wake_id or "").strip()
    if not wid:
        return False
    with _LOCK:
        rec = _WAKES.pop(wid, None)
    if rec is None:
        return False
    logger.info("agent_wake: cancelled wake_id=%s session=%s", wid, rec.session_id)
    _sync_feishu_push_watch_file()
    return True


def list_wakes(*, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    sid = str(session_id or "").strip() if session_id else ""
    with _LOCK:
        items = list(_WAKES.values())
    if sid:
        items = [w for w in items if w.session_id == sid]
    items.sort(key=lambda w: w.fire_at)
    return [_wake_to_dict(w) for w in items]


def session_has_deferred_agent_wake(
    session_id: str,
    *,
    min_lead_seconds: float = 5.0,
) -> bool:
    """Session 是否已有 Agent 主动登记的 future wake（不含 goal-check 系统续跑）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    now = _now()
    threshold = now + max(0.0, float(min_lead_seconds))
    with _LOCK:
        for w in _WAKES.values():
            if w.fired or w.session_id != sid:
                continue
            if str(w.label or "").strip() == "goal-check":
                continue
            if w.fire_at > threshold:
                return True
    return False


def cancel_pending_wakes(
    session_id: str,
    *,
    label: Optional[str] = None,
) -> int:
    """取消 session 上尚未触发的 wake；可按 label 过滤。返回取消数量。"""
    sid = str(session_id or "").strip()
    if not sid:
        return 0
    with _LOCK:
        to_cancel = [
            wid
            for wid, w in list(_WAKES.items())
            if not w.fired
            and w.session_id == sid
            and (label is None or str(w.label or "").strip() == label)
        ]
    count = 0
    for wid in to_cancel:
        if cancel_wake(wid):
            count += 1
    return count


def _wake_to_dict(w: _ScheduledWake) -> Dict[str, Any]:
    return {
        "wake_id": w.wake_id,
        "session_id": w.session_id,
        "fire_at": w.fire_at,
        "fire_at_iso": time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(w.fire_at)
        ),
        "seconds_until": max(0.0, w.fire_at - _now()),
        "message": w.message,
        "label": w.label,
        "created_at": w.created_at,
        "frontend_id": w.frontend_id,
        "source": w.source,
        "user_id": w.user_id,
        "metadata": dict(w.metadata),
        "fired": w.fired,
        "staged": w.staged,
    }


def poll_due_wakes(*, max_items: int = 20) -> List[Dict[str, Any]]:
    """返回已到点且尚未投递的唤醒（不移除注册表）。"""
    if max_items <= 0:
        return []
    now = _now()
    with _LOCK:
        candidates = [
            w
            for w in _WAKES.values()
            if not w.fired and not w.staged and w.fire_at <= now
        ]
    candidates.sort(key=lambda w: w.fire_at)
    return [_wake_to_dict(w) for w in candidates[:max_items]]


def format_wake_notification(wake: Dict[str, Any]) -> str:
    """构建注入会话的唤醒正文。"""
    label = str(wake.get("label") or "").strip()
    message = str(wake.get("message") or "").strip()
    wid = str(wake.get("wake_id") or "")
    header = "[定时唤醒]"
    if label:
        header = f"{header} {label}"
    lines = [header, message]
    if wid:
        lines.append(f"(wake_id={wid})")
    return "\n".join(lines)


def _get_wake(wake_id: str) -> Optional[_ScheduledWake]:
    with _LOCK:
        return _WAKES.get(str(wake_id or "").strip())


def _mark_fired(wake_id: str) -> None:
    with _LOCK:
        _WAKES.pop(str(wake_id or "").strip(), None)
    _sync_feishu_push_watch_file()


def _feishu_watch_file_path() -> Path:
    from system.automation.repositories import _automation_base_dir

    return _automation_base_dir() / "feishu_wake_push_watch.json"


def list_feishu_push_watch_targets() -> List[Dict[str, Any]]:
    """供飞书 FeishuPushForwarder 在唤醒触发前持续 poll_push。"""
    now = _now()
    with _LOCK:
        wakes = list(_WAKES.values())
    seen: set[tuple[str, str]] = set()
    out: List[Dict[str, Any]] = []
    for w in wakes:
        if w.fired:
            continue
        cid = str(w.metadata.get("feishu_chat_id") or "").strip()
        sid = str(w.session_id or "").strip()
        if not cid or not sid.startswith("feishu:"):
            continue
        key = (sid, cid)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "session_id": sid,
                "chat_id": cid,
                "fire_at": w.fire_at,
                "ttl_seconds": max(60.0, float(w.fire_at) - now + 120.0),
            }
        )
    return out


def _sync_feishu_push_watch_file() -> None:
    try:
        path = _feishu_watch_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = list_feishu_push_watch_targets()
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("agent_wake: sync feishu watch file failed: %s", exc)


def load_feishu_push_watch_targets_from_file() -> List[Dict[str, Any]]:
    """飞书进程侧读取 daemon 写入的待唤醒 push 注册表。"""
    try:
        path = _feishu_watch_file_path()
        if not path.is_file():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        out: List[Dict[str, Any]] = []
        now = _now()
        for item in raw:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("session_id") or "").strip()
            cid = str(item.get("chat_id") or "").strip()
            if not sid or not cid:
                continue
            ttl = item.get("ttl_seconds")
            if ttl is None:
                fire_at = float(item.get("fire_at") or 0.0)
                ttl = max(60.0, fire_at - now + 120.0)
            out.append(
                {
                    "session_id": sid,
                    "chat_id": cid,
                    "ttl_seconds": max(60.0, float(ttl)),
                }
            )
        return out
    except Exception as exc:
        logger.debug("agent_wake: load feishu watch file failed: %s", exc)
        return []


def _mark_staged(wake_id: str) -> None:
    wid = str(wake_id or "").strip()
    with _LOCK:
        rec = _WAKES.get(wid)
        if rec is not None:
            rec.staged = True


def _reset_staged(wake_id: str) -> None:
    wid = str(wake_id or "").strip()
    with _LOCK:
        rec = _WAKES.get(wid)
        if rec is not None:
            rec.staged = False


def confirm_wake_delivered(wake_id: str) -> None:
    """内核成功跑完 inject 的唤醒 turn 后调用，从注册表移除。"""
    _mark_fired(wake_id)


def abort_wake_delivery(wake_id: str) -> None:
    """inject 被 skip/取消时尚未真正投递：清除 staged 以便 poll 重试。"""
    _reset_staged(wake_id)


def stage_wake_notification(session_id: str, text: str, *, wake_id: str) -> None:
    sid = str(session_id or "").strip()
    wid = str(wake_id or "").strip()
    if not sid or not text or not wid:
        return
    with _LOCK:
        rec = _WAKES.get(wid)
        if rec is not None:
            rec.staged = True
        wake_snapshot = _wake_to_dict(rec) if rec is not None else None
        _PENDING_BY_SESSION.setdefault(sid, []).append(
            {"text": text, "wake_id": wid, "wake": wake_snapshot}
        )


def flush_pending_wakes_for_session(session_id: str) -> None:
    """父会话 inflight==0 时 flush 暂存的唤醒通知。"""
    sid = str(session_id or "").strip()
    if not sid:
        return
    with _LOCK:
        pending = _PENDING_BY_SESSION.pop(sid, [])
    if not pending:
        return
    from agent_core.tools.bash_job_notify import get_notify_dependencies

    deps = get_notify_dependencies()
    scheduler = deps.get("scheduler")
    core_pool = deps.get("core_pool")
    for item in pending:
        text = str(item.get("text") or "")
        wake = item.get("wake")
        wid = str(item.get("wake_id") or "")
        try:
            deliver_wake_via_inject(
                wake=wake if isinstance(wake, dict) else {"wake_id": wid, "session_id": sid},
                text=text,
                scheduler=scheduler,
                core_pool=core_pool,
            )
        except Exception as exc:
            logger.warning(
                "agent_wake: flush deliver failed session=%s wake=%s: %s",
                sid,
                wid,
                exc,
            )
            if wid:
                _reset_staged(wid)


def deliver_wake_via_inject(
    *,
    wake: Dict[str, Any],
    text: Optional[str] = None,
    scheduler: Optional["KernelScheduler"] = None,
    core_pool: Optional["CorePool"] = None,
) -> bool:
    """
    将定时唤醒通过 inject_turn 投递到原会话。

    - 父会话 inflight>0 时暂存，由 flush_pending_wakes_for_session 稍后投递。
    """
    sid = str(wake.get("session_id") or "").strip()
    wid = str(wake.get("wake_id") or "").strip()
    body = str(text or format_wake_notification(wake)).strip()
    if not sid or not body:
        return False

    from agent_core.tools.bash_job_notify import (
        build_feishu_inject_metadata,
        get_notify_dependencies,
    )

    if scheduler is None:
        scheduler = get_notify_dependencies().get("scheduler")
    if core_pool is None:
        core_pool = get_notify_dependencies().get("core_pool")

    if scheduler is None:
        logger.warning(
            "agent_wake: scheduler not set, cannot inject session=%s wake=%s",
            sid,
            wid or "?",
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
        if wid:
            rec = _get_wake(wid)
            if rec is not None and rec.staged:
                return True
        stage_wake_notification(sid, body, wake_id=wid)
        logger.info(
            "agent_wake: staged session=%s wake=%s inflight=%d",
            sid,
            wid or "?",
            inflight,
        )
        return True

    from agent_core.kernel_interface.action import KernelRequest

    wake_meta = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    chat_hint = str(wake_meta.get("feishu_chat_id") or "").strip() or None
    inject_md: Dict[str, Any] = {
        "source": wake.get("source") or "cli",
        "user_id": wake.get("user_id") or "root",
        "_wake_id": wid,
    }
    feishu_md = build_feishu_inject_metadata(
        sid,
        core_pool,
        chat_id_hint=chat_hint,
        markdown_header_title="定时唤醒",
    )
    if feishu_md:
        inject_md.update(feishu_md)

    request = KernelRequest.create(
        text=body,
        session_id=sid,
        frontend_id="agent_wake",
        priority=-1,
        metadata=inject_md,
    )
    logger.info(
        "agent_wake: delivered session=%s wake=%s staged=false",
        sid,
        wid or "?",
    )
    try:
        scheduler.inject_turn(request)
    except Exception as exc:
        logger.warning("agent_wake: inject_turn failed session=%s: %s", sid, exc)
        if wid:
            _reset_staged(wid)
        return False

    if wid:
        _mark_staged(wid)
    return True


def clear_all_wakes_for_tests() -> None:
    with _LOCK:
        _WAKES.clear()
        _PENDING_BY_SESSION.clear()
    _sync_feishu_push_watch_file()
