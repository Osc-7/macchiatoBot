"""request_permission：阻塞等待人类在前端批准或拒绝（及可选持久写前缀）。"""

from __future__ import annotations

import json
from typing import Any, Optional

import agent_core.config as _config_mod
from agent_core.permissions.broker import PathGrant, PermissionBroker, PermissionRequest
from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult
from agent_core.tools.permission_path_infer import (
    infer_readable_prefix_from_details,
    infer_writable_prefix_from_details,
)


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


def _path_grants_from_details(details: Any) -> list[PathGrant]:
    if isinstance(details, str):
        try:
            raw = json.loads(details)
        except json.JSONDecodeError:
            raw = None
    else:
        raw = details
    if not isinstance(raw, dict):
        return []
    grants: list[PathGrant] = []
    for item in raw.get("path_grants") or []:
        grant = PathGrant.from_payload(item)
        if grant is not None:
            grants.append(grant)
    return grants


class RequestPermissionTool(BaseTool):
    """挂起当前 turn，直到 resolve_permission 或超时。"""

    @property
    def name(self) -> str:
        return "request_permission"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""少数复杂场景下，主动请求人类批准权限。

用于：
- 需要写入全局配置目录（如 ~/.agents/skills）
- 需要读取工作区外的宿主机目录（如指定的共享只读目录）
- 需要一次性或长期扩展可写路径前缀
- 对复杂权限需求先向人类解释原因

调用后进程会等待人类在前端选择允许/拒绝；超时默认视为拒绝。

注意：bash、read_file、write_file、modify_file 已能在命中权限边界时自动申请并在批准后继续执行。
优先直接调用目标工具；只有当你需要先解释一组复杂授权或没有具体目标工具可调用时，才使用本工具。

**是否把路径加入持久白名单仅由人类决定**（飞书卡片上 Once vs Always）。本工具**没有**「是否持久化」类参数；你只能在 summary/details 里说明需求，**不得**在对话里假装已获永久白名单。批准后请看返回的 data.persist_acl（人类选择结果）。""",
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
                    description="类别：bash_write_outside_workspace、file_write、file_read、bash_dangerous_command、other",
                    required=False,
                ),
                ToolParameter(
                    name="details",
                    type="string",
                    description="可选 JSON：path、path_prefix、path_grants、reason 等；用于推断待审批路径前缀。`file_read` 会登记到只读白名单，`file_write` / `bash_write_outside_workspace` 会登记到可写白名单。**是否持久加入白名单不由本字段决定**，由人类在飞书卡片上选择（返回 data.persist_acl）",
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
                "优先直接调用 bash/read_file/write_file/modify_file；这些工具会在需要时自动申请权限",
                "仅在需要先解释复杂授权或没有具体目标工具可调用时主动使用",
                "file_read：适用于 read_file 读取用户根外路径被拒绝时申请只读前缀，不会附带写权限",
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
        profile_mode = str(exec_ctx.get("profile_mode") or "full").strip().lower()
        if profile_mode == "sub":
            parent_session_id = str(exec_ctx.get("parent_session_id") or "").strip()
            broker_target = parent_session_id or "父会话"
            return ToolResult(
                success=False,
                error="SUBAGENT_PERMISSION_BROKER_REQUIRED",
                message=(
                    "子 Agent 不允许直接 request_permission。"
                    "请先用 send_message_to_agent 向父会话申请，由父会话统一发起审批。"
                ),
                data={
                    "broker_required": True,
                    "parent_session_id": parent_session_id,
                    "suggested_send_message": {
                        "session_id": broker_target,
                        "content": (
                            "[PERMISSION_BROKER_REQUEST]\n"
                            f"summary={summary}\n"
                            f"kind={kind}\n"
                            f"details={details if details is not None else ''}\n"
                            "请父会话决定是否发起 request_permission，并在批准后返回可执行指令。"
                        ),
                        "require_reply": True,
                    },
                },
            )
        try:
            timeout = float(timeout_s) if timeout_s is not None else 300.0
        except (TypeError, ValueError):
            timeout = 300.0

        cfg = _config_mod.get_config()
        kind_l = kind.strip().lower().replace("-", "_")
        grants = _path_grants_from_details(details)
        if not grants:
            infer = (
                infer_readable_prefix_from_details
                if kind_l in ("file_read", "file_read_outside_workspace")
                else infer_writable_prefix_from_details
            )
            inferred = infer(details, config=cfg, exec_ctx=dict(exec_ctx))
            if inferred:
                grants.append(
                    PathGrant(
                        path_prefix=inferred,
                        access_mode=(
                            "read"
                            if kind_l in ("file_read", "file_read_outside_workspace")
                            else "write"
                        ),
                        reason="explicit request_permission",
                    )
                )

        bc = _bash_command_from_details(details)
        broker = PermissionBroker(cfg)
        broker_result = await broker.request(
            PermissionRequest(
                tool_name="request_permission",
                kind=kind_l,
                summary=summary,
                details=details,
                command=bc,
                risk_reasons=(
                    ["危险 bash 命令"]
                    if kind_l in ("bash_dangerous_command", "bash_dangerous")
                    else []
                ),
                path_grants=grants,
                timeout_seconds=timeout,
                auto_execute_after_approval=False,
                exec_ctx=dict(exec_ctx),
            )
        )

        if not broker_result.allowed:
            if broker_result.error == "PERMISSION_TIMEOUT":
                return ToolResult(
                    success=False,
                    error="PERMISSION_TIMEOUT",
                    message=broker_result.message,
                    data={"permission_id": broker_result.permission_id},
                )
            if broker_result.error == "PERMISSION_CANCELLED":
                return ToolResult(
                    success=False,
                    error="PERMISSION_CANCELLED",
                    message=broker_result.message,
                    data={"permission_id": broker_result.permission_id},
                )
            if broker_result.error == "PERMISSION_CLARIFY":
                ui = broker_result.user_instruction
                msg = (
                    "用户未批准本次权限。飞书卡片补充说明：\n"
                    + ui
                    + "\n请据此澄清后再次调用 request_permission。"
                    if ui
                    else "用户未批准本次权限（飞书卡片未填写说明）。请结合对话澄清后再次调用 request_permission。"
                )
                return ToolResult(
                    success=False,
                    error="PERMISSION_CLARIFY",
                    message=msg,
                    data={
                        "permission_id": broker_result.permission_id,
                        "user_instruction": ui,
                    },
                )
            if broker_result.error == "ACL_PERSIST_FAILED":
                return ToolResult(
                    success=False,
                    error="ACL_PERSIST_FAILED",
                    message=broker_result.message,
                    data={"permission_id": broker_result.permission_id},
                )
            return ToolResult(
                success=False,
                error=broker_result.error or "PERMISSION_DENIED",
                message=broker_result.message,
                data={"permission_id": broker_result.permission_id},
            )

        if kind_l in ("bash_dangerous_command", "bash_dangerous"):
            from agent_core.permissions.bash_danger_approvals import (
                register_bash_danger_grant,
            )

            if bc:
                register_bash_danger_grant(broker_result.permission_id, bc)

        grants_payload = [g.to_payload() for g in broker_result.applied_grants]
        prefix_msg = ""
        if broker_result.applied_grants:
            label = "已持久化" if broker_result.persist_acl else "本次进程内允许"
            prefix_msg = (
                " "
                + label
                + "路径前缀: "
                + ", ".join(g.path_prefix for g in broker_result.applied_grants)
            )

        return ToolResult(
            success=True,
            message="已批准。" + prefix_msg,
            data={
                "permission_id": broker_result.permission_id,
                "path_prefix": (
                    broker_result.applied_grants[0].path_prefix
                    if broker_result.applied_grants
                    else None
                ),
                "path_grants": grants_payload,
                "note": broker_result.note,
                "persist_acl": broker_result.persist_acl,
                "access_mode": (
                    broker_result.applied_grants[0].access_mode
                    if broker_result.applied_grants
                    else (
                        "read"
                        if kind_l in ("file_read", "file_read_outside_workspace")
                        else "write"
                    )
                ),
            },
        )
