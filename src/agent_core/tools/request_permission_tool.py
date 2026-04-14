"""request_permission：阻塞等待人类在前端批准或拒绝（及可选持久写前缀）。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

import agent_core.config as _config_mod
from agent_core.permissions.wait_registry import (
    PermissionDecision,
    notify_permission_pending,
    register_permission_wait,
)
from agent_core.agent.writable_ephemeral_grants import add_ephemeral_writable_prefix
from agent_core.agent.writable_roots_store import append_user_writable_prefix
from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from agent_core.tools.permission_path_infer import infer_writable_prefix_from_details


def _bash_command_from_details(details: Any) -> Optional[str]:
    """从 details（JSON 字符串或 dict）解析 bash 危险命令的 command 字段。"""
    if details is None:
        return None
    if isinstance(details, dict):
        cmd = details.get("command")
        return str(cmd).strip() if cmd is not None and str(cmd).strip() else None
    if isinstance(details, str):
        s = details.strip()
        if not s:
            return None
        try:
            d = json.loads(s)
        except json.JSONDecodeError:
            return None
        if isinstance(d, dict):
            cmd = d.get("command")
            return str(cmd).strip() if cmd is not None and str(cmd).strip() else None
    return None


class RequestPermissionTool(BaseTool):
    """挂起当前 turn，直到 resolve_permission 或超时。"""

    @property
    def name(self) -> str:
        return "request_permission"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""当 bash / 文件写入因工作区隔离或缺少可写路径被拒绝时，请求人类批准。

用于：
- 需要写入全局配置目录（如 ~/.agents/skills）
- 需要一次性或长期扩展可写路径前缀
- 危险 bash（rm -rf、sudo 等）：kind=bash_dangerous_command，details 为 JSON，必须含 command 字段（与将执行的命令完全一致）。人类批准后，用返回的 data.permission_id 与同一 command 再调 bash（一次性）

调用后进程会等待人类在前端选择允许/拒绝；超时默认视为拒绝。

**是否把路径加入持久白名单仅由人类决定**（飞书卡片上「本次有效」vs「加白名单」）。本工具**没有**「是否持久化」类参数；你只能在 summary/details 里说明需求，**不得**在对话里假装已获永久白名单。批准后请看返回的 data.persist_acl（人类选择结果）。""",
            parameters=[
                ToolParameter(
                    name="summary",
                    type="string",
                    description="人类可读摘要：需要什么权限、为何需要",
                    required=True,
                ),
                ToolParameter(
                    name="kind",
                    type="string",
                    description="类别：bash_write_outside_workspace、file_write、bash_dangerous_command、other",
                    required=False,
                ),
                ToolParameter(
                    name="details",
                    type="string",
                    description='可选 JSON：path、path_prefix、reason 等；用于推断待审批路径前缀。**是否持久加入白名单不由本字段决定**，由人类在飞书卡片上选择（返回 data.persist_acl）',
                    required=False,
                ),
                ToolParameter(
                    name="timeout_seconds",
                    type="number",
                    description="等待秒数，默认 300",
                    required=False,
                ),
            ],
            examples=[],
            usage_notes=[
                "仅在工具返回 WORKSPACE_WRITE_DENIED、CONFIRMATION_REQUIRED 等且需要人类决策时调用",
                "bash_dangerous_command：details 必须含 command，与后续 bash 的 command 逐字一致",
                "持久白名单 / 仅本次放行：仅人类在飞书卡片上选；Agent 无 persist 参数，以工具返回 data.persist_acl 为准",
            ],
            tags=["权限", "审批"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        exec_ctx = kwargs.pop("__execution_context__", None) or {}
        summary = str(kwargs.get("summary") or "").strip()
        if not summary:
            return ToolResult(
                success=False,
                error="MISSING_SUMMARY",
                message="缺少 summary",
            )
        kind = str(kwargs.get("kind") or "").strip() or "other"
        details = kwargs.get("details")
        timeout_s = kwargs.get("timeout_seconds")
        try:
            timeout = float(timeout_s) if timeout_s is not None else 300.0
        except (TypeError, ValueError):
            timeout = 300.0

        cfg = _config_mod.get_config()
        cmd_cfg = cfg.command_tools

        pid, fut = register_permission_wait()
        feishu_cid = str(exec_ctx.get("feishu_chat_id") or "").strip()
        payload: Dict[str, Any] = {
            "summary": summary,
            "kind": kind,
            "details": details,
            "timeout_seconds": timeout,
            "memory_owner": exec_ctx.get("memory_owner"),
            "session_id": exec_ctx.get("session_id"),
            "source": exec_ctx.get("source"),
            "user_id": exec_ctx.get("user_id"),
        }
        if feishu_cid:
            payload["feishu_chat_id"] = feishu_cid
        inferred = infer_writable_prefix_from_details(
            details, config=cfg, exec_ctx=dict(exec_ctx)
        )
        if inferred:
            payload["path_prefix"] = inferred
        notify_permission_pending(pid, payload)

        try:
            decision: PermissionDecision = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return ToolResult(
                success=False,
                error="PERMISSION_TIMEOUT",
                message="等待人类批准超时，视为拒绝",
                data={"permission_id": pid},
            )
        except asyncio.CancelledError as exc:
            return ToolResult(
                success=False,
                error="PERMISSION_CANCELLED",
                message=str(exc),
                data={"permission_id": pid},
            )

        if getattr(decision, "clarify_requested", False):
            ui = str(getattr(decision, "user_instruction", None) or "").strip()
            if ui:
                msg = (
                    "用户未批准本次权限。飞书卡片补充说明：\n"
                    + ui
                    + "\n请据此澄清后再次调用 request_permission。"
                )
            else:
                msg = (
                    "用户未批准本次权限（飞书卡片未填写说明）。"
                    "请结合对话澄清后再次调用 request_permission。"
                )
            return ToolResult(
                success=False,
                error="PERMISSION_CLARIFY",
                message=msg,
                data={"permission_id": pid, "user_instruction": ui},
            )

        if not decision.allowed:
            return ToolResult(
                success=False,
                error="PERMISSION_DENIED",
                message=decision.note or "人类拒绝了该权限请求",
                data={"permission_id": pid},
            )

        kind_l = kind.strip().lower().replace("-", "_")
        if kind_l in ("bash_dangerous_command", "bash_dangerous"):
            from agent_core.permissions.bash_danger_approvals import (
                register_bash_danger_grant,
            )

            bc = _bash_command_from_details(details)
            if bc:
                register_bash_danger_grant(pid, bc)

        prefix_msg = ""
        persist = bool(getattr(decision, "persist_acl", False))
        if decision.path_prefix and str(decision.path_prefix).strip():
            pfx = str(decision.path_prefix).strip()
            try:
                src = str(exec_ctx.get("source") or "cli").strip() or "cli"
                uid = str(exec_ctx.get("user_id") or "root").strip() or "root"
                if persist:
                    append_user_writable_prefix(
                        cmd_cfg.acl_base_dir, src, uid, pfx, config=cfg
                    )
                    prefix_msg = f" 已持久化可写前缀: {pfx}"
                else:
                    add_ephemeral_writable_prefix(src, uid, pfx, config=cfg)
                    prefix_msg = f" 本次进程内允许该前缀写入（未写入永久白名单）: {pfx}"
            except Exception as exc:
                return ToolResult(
                    success=False,
                    error="ACL_PERSIST_FAILED",
                    message=f"批准但应用可写路径失败: {exc}",
                    data={"permission_id": pid},
                )

        return ToolResult(
            success=True,
            message="已批准。" + prefix_msg,
            data={
                "permission_id": pid,
                "path_prefix": decision.path_prefix,
                "note": decision.note,
                "persist_acl": persist,
            },
        )
