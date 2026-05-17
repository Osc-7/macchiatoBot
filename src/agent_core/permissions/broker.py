"""Shared human permission broker for tools.

Tools call this module when they have already determined the exact operation
that needs human approval. The broker owns the wait/notify/apply-grants flow so
bash, file tools, and the explicit request_permission tool share one behavior.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from agent_core.agent.path_grants import (
    add_ephemeral_path_prefix,
    append_user_path_prefix,
)
from agent_core.config import Config, get_config
from agent_core.permissions.wait_registry import (
    PermissionDecision,
    cancel_permission_wait,
    notify_permission_pending,
    register_permission_wait,
)

AccessMode = Literal["read", "write"]


@dataclass(frozen=True)
class PathGrant:
    """A path prefix the user may approve for read/write access."""

    path_prefix: str
    access_mode: AccessMode = "write"
    reason: str = ""

    def to_payload(self) -> Dict[str, str]:
        payload = {
            "path_prefix": self.path_prefix,
            "access_mode": self.access_mode,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload

    @classmethod
    def from_payload(cls, raw: Any) -> Optional["PathGrant"]:
        if not isinstance(raw, dict):
            return None
        pfx = str(raw.get("path_prefix") or "").strip()
        if not pfx:
            return None
        mode = str(raw.get("access_mode") or "write").strip().lower()
        if mode not in ("read", "write"):
            mode = "write"
        return cls(
            path_prefix=pfx,
            access_mode=mode,  # type: ignore[arg-type]
            reason=str(raw.get("reason") or "").strip(),
        )


@dataclass(frozen=True)
class PermissionRequest:
    """Complete description of one human approval request."""

    tool_name: str
    kind: str
    summary: str
    details: Any = None
    command: Optional[str] = None
    cwd: Optional[str] = None
    risk_reasons: List[str] = field(default_factory=list)
    path_grants: List[PathGrant] = field(default_factory=list)
    timeout_seconds: float = 300.0
    auto_execute_after_approval: bool = False
    exec_ctx: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PermissionBrokerResult:
    """Result returned to the tool that requested approval."""

    allowed: bool
    permission_id: str
    error: Optional[str] = None
    message: str = ""
    clarify_requested: bool = False
    user_instruction: str = ""
    applied_grants: List[PathGrant] = field(default_factory=list)
    persist_acl: bool = False
    note: Optional[str] = None


def _details_to_payload(details: Any) -> Any:
    if isinstance(details, (dict, list)):
        return details
    if isinstance(details, str):
        s = details.strip()
        if not s:
            return None
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return details
    return details


def _path_grants_from_decision_or_request(
    decision: PermissionDecision,
    request: PermissionRequest,
) -> List[PathGrant]:
    raw_grants = getattr(decision, "path_grants", None)
    grants: List[PathGrant] = []
    if raw_grants:
        for raw in raw_grants:
            grant = PathGrant.from_payload(raw)
            if grant is not None:
                grants.append(grant)
    if not grants and request.path_grants:
        grants.extend(request.path_grants)
    if not grants and decision.path_prefix:
        mode: AccessMode = (
            "read"
            if request.kind
            in (
                "file_read",
                "file_read_outside_workspace",
            )
            else "write"
        )
        grants.append(PathGrant(str(decision.path_prefix), mode))
    return grants


def _source_user(exec_ctx: Dict[str, Any]) -> tuple[str, str]:
    src = str(exec_ctx.get("source") or "cli").strip() or "cli"
    uid = str(exec_ctx.get("user_id") or "root").strip() or "root"
    return src, uid


class PermissionBroker:
    """Request human approval and apply selected path grants."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self._config = config or get_config()

    async def request(self, request: PermissionRequest) -> PermissionBrokerResult:
        timeout = max(0.1, float(request.timeout_seconds or 300.0))
        pid, fut = register_permission_wait()
        payload = self._build_payload(pid, request, timeout)
        notify_permission_pending(pid, payload)

        try:
            decision: PermissionDecision = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            cancel_permission_wait(pid, reason="timeout")
            return PermissionBrokerResult(
                allowed=False,
                permission_id=pid,
                error="PERMISSION_TIMEOUT",
                message="等待人类批准超时，视为拒绝",
            )
        except asyncio.CancelledError as exc:
            cancel_permission_wait(pid, reason=str(exc) or "cancelled")
            return PermissionBrokerResult(
                allowed=False,
                permission_id=pid,
                error="PERMISSION_CANCELLED",
                message=str(exc),
            )

        if getattr(decision, "clarify_requested", False):
            ui = str(getattr(decision, "user_instruction", None) or "").strip()
            msg = "用户未批准本次权限。"
            if ui:
                msg += "补充说明：\n" + ui
            return PermissionBrokerResult(
                allowed=False,
                permission_id=pid,
                error="PERMISSION_CLARIFY",
                message=msg,
                clarify_requested=True,
                user_instruction=ui,
                note=decision.note,
            )

        if not decision.allowed:
            return PermissionBrokerResult(
                allowed=False,
                permission_id=pid,
                error="PERMISSION_DENIED",
                message=decision.note or "人类拒绝了该权限请求",
                note=decision.note,
            )

        persist = bool(getattr(decision, "persist_acl", False))
        grants = _path_grants_from_decision_or_request(decision, request)
        try:
            applied = self._apply_path_grants(grants, request.exec_ctx, persist)
        except Exception as exc:
            return PermissionBrokerResult(
                allowed=False,
                permission_id=pid,
                error="ACL_PERSIST_FAILED",
                message=f"批准但应用路径权限失败: {exc}",
                persist_acl=persist,
                note=decision.note,
            )

        return PermissionBrokerResult(
            allowed=True,
            permission_id=pid,
            message="已批准",
            applied_grants=applied,
            persist_acl=persist,
            note=decision.note,
        )

    def _build_payload(
        self,
        pid: str,
        request: PermissionRequest,
        timeout: float,
    ) -> Dict[str, Any]:
        exec_ctx = dict(request.exec_ctx or {})
        payload: Dict[str, Any] = {
            "summary": request.summary,
            "kind": request.kind,
            "details": _details_to_payload(request.details),
            "timeout_seconds": timeout,
            "memory_owner": exec_ctx.get("memory_owner"),
            "session_id": exec_ctx.get("session_id"),
            "source": exec_ctx.get("source"),
            "user_id": exec_ctx.get("user_id"),
            "tool_name": request.tool_name,
            "auto_execute_after_approval": request.auto_execute_after_approval,
            "permission_id": pid,
        }
        if request.command:
            payload["command"] = request.command
        if request.cwd:
            payload["cwd"] = request.cwd
        if request.risk_reasons:
            payload["risk_reasons"] = list(request.risk_reasons)
        if request.path_grants:
            payload["path_grants"] = [g.to_payload() for g in request.path_grants]
            payload["path_prefix"] = request.path_grants[0].path_prefix
        feishu_cid = str(exec_ctx.get("feishu_chat_id") or "").strip()
        if feishu_cid:
            payload["feishu_chat_id"] = feishu_cid
        return payload

    def _apply_path_grants(
        self,
        grants: List[PathGrant],
        exec_ctx: Dict[str, Any],
        persist: bool,
    ) -> List[PathGrant]:
        if not grants:
            return []
        src, uid = _source_user(exec_ctx)
        applied: List[PathGrant] = []
        for grant in grants:
            pfx = str(Path(grant.path_prefix).expanduser().resolve())
            normalized = PathGrant(
                path_prefix=pfx,
                access_mode=grant.access_mode,
                reason=grant.reason,
            )
            if persist:
                append_user_path_prefix(
                    self._config.command_tools.acl_base_dir,
                    src,
                    uid,
                    pfx,
                    access_mode=grant.access_mode,
                    config=self._config,
                )
            else:
                add_ephemeral_path_prefix(
                    src,
                    uid,
                    pfx,
                    access_mode=grant.access_mode,
                    config=self._config,
                )
            applied.append(normalized)
        return applied
