"""
BashTool -- 持久化 bash 会话工具。

绑定到 AgentCore 的 BashRuntime，作为 meta tool 在 AgentCore.__init__ 中自注册
（与 search_tools / call_tool 同模式）。

对 LLM 暴露的参数对齐 Anthropic 官方 Bash tool API:
  - command (string): 要执行的命令
  - restart (bool): 是否重启 bash 会话
  - timeout (number): 超时秒数
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult

if TYPE_CHECKING:
    from agent_core.bash_runtime import BashRuntime
    from agent_core.bash_security import BashSecurity
    from agent_core.kernel_interface.profile import CoreProfile


class BashTool(BaseTool):
    """
    持久化 bash 会话工具，绑定到当前 AgentCore。

    每个 AgentCore 拥有独立的 BashRuntime 实例，命令在同一 bash
    进程中执行，环境变量、工作目录等在整个会话期间保持。
    """

    def __init__(
        self,
        bash: "BashRuntime",
        security: "BashSecurity",
    ) -> None:
        self._bash = bash
        self._security = security

    @property
    def name(self) -> str:
        return "bash"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""在持久化 bash 会话中执行命令。

这是一个持久化的 bash 会话：环境变量、工作目录、命令历史在整个对话期间保持。
前一条命令创建的文件、设置的变量，在后续命令中仍然可用。

当你想要：
- 查看目录/文件信息（ls、pwd、find 等）
- 运行脚本或测试命令（pytest、python script.py 等）
- 查询 Git 状态、构建状态等开发信息
- 安装依赖、设置环境变量（pip install、export 等）
- 执行多步工作流（cd 到目录 → 安装 → 构建 → 测试）

注意事项：
- 危险操作（rm -rf、chmod -R、sudo 等）须先调用 request_permission（kind=bash_dangerous_command，
  details 为含 command 字段的 JSON）；人类批准后，用返回的 permission_id 与**完全相同**的 command 再调 bash（一次性）
- 超时后 bash 会话会自动重启
- 使用 restart=true 可手动重启 bash 会话（清除所有状态）""",
            parameters=[
                ToolParameter(
                    name="command",
                    type="string",
                    description="要执行的 shell 命令",
                    required=False,
                ),
                ToolParameter(
                    name="restart",
                    type="boolean",
                    description="设为 true 时重启 bash 会话（清除所有环境变量和工作目录状态）",
                    required=False,
                    default=False,
                ),
                ToolParameter(
                    name="timeout",
                    type="number",
                    description="超时时间（秒），超时后命令会被终止且 bash 会话自动重启",
                    required=False,
                ),
                ToolParameter(
                    name="permission_id",
                    type="string",
                    description=(
                        "危险命令专用：人类批准 request_permission(bash_dangerous_command) 后返回的 "
                        "permission_id；须与批准时 details 中的 command 完全一致，一次性有效"
                    ),
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "查看当前目录文件",
                    "params": {"command": "ls -la"},
                },
                {
                    "description": "多步工作流：进入目录并运行测试",
                    "params": {"command": "cd tests && pytest -q"},
                },
                {
                    "description": "设置环境变量（后续命令仍可用）",
                    "params": {"command": "export MY_VAR=hello"},
                },
                {
                    "description": "重启 bash 会话",
                    "params": {"restart": True},
                },
            ],
            usage_notes=[
                "这是持久化 bash 会话：cd、export 等在后续命令中生效",
                "危险命令须先 request_permission，人类批准后用 permission_id + 同一 command 执行一次",
                "超时会导致 bash 会话重启，所有状态清空",
                "使用 restart=true 可手动重置会话",
            ],
            tags=["命令", "终端", "bash", "执行"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        exec_ctx = kwargs.pop("__execution_context__", None) or {}

        restart = kwargs.get("restart", False)
        if restart:
            await self._bash.restart()
            return ToolResult(
                success=True,
                message="Bash 会话已重启，所有环境变量和工作目录状态已清除",
            )

        command = str(kwargs.get("command", "")).strip()
        if not command:
            return ToolResult(
                success=False,
                error="MISSING_COMMAND",
                message="缺少必需参数: command（或使用 restart=true 重启会话）",
            )

        # 忽略模型可能仍传入的 confirm（不再具有效力；批准仅能通过 request_permission）
        kwargs.pop("confirm", None)

        from agent_core.permissions.bash_danger_approvals import (
            consume_bash_danger_grant,
        )

        perm_id = kwargs.get("permission_id")
        perm_str = str(perm_id).strip() if perm_id is not None else ""
        confirmed = (
            bool(perm_str) and consume_bash_danger_grant(perm_str, command)
        )

        if self._security and getattr(self._security, "_workspace_jail_root", None):
            self._security.refresh_write_roots_from_config(
                str(exec_ctx.get("source") or "cli"),
                str(exec_ctx.get("user_id") or "root"),
            )

        profile = self._resolve_profile(exec_ctx)
        verdict = self._security.check(
            command,
            profile=profile,
            confirmed=confirmed,
        )

        if verdict.denied:
            data = None
            if verdict.error_code == "WORKSPACE_WRITE_DENIED":
                data = {
                    "suggested_tool": "request_permission",
                    "denied_command": command,
                    "error_code": verdict.error_code,
                }
            return ToolResult(
                success=False,
                error=verdict.error_code,
                message=verdict.reason,
                data=data,
            )

        if verdict.needs_confirmation:
            import json as _json

            return ToolResult(
                success=False,
                error=verdict.error_code,
                message=verdict.reason,
                data={
                    "suggested_tool": "request_permission",
                    "permission_kind": "bash_dangerous_command",
                    "details_json": _json.dumps(
                        {"command": command}, ensure_ascii=False
                    ),
                },
            )

        timeout = kwargs.get("timeout")
        if timeout is not None:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError):
                return ToolResult(
                    success=False,
                    error="INVALID_TIMEOUT",
                    message="timeout 必须是数字（秒）",
                )

        result = await self._bash.execute(command, timeout=timeout)

        data = {
            "command": result.command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.exit_code,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
        }

        if result.timed_out:
            return ToolResult(
                success=False,
                data=data,
                error="COMMAND_TIMEOUT",
                message=f"命令执行超时，进程已终止且 bash 会话已重启",
            )

        if result.exit_code == 0:
            return ToolResult(success=True, data=data, message="命令执行成功")

        return ToolResult(
            success=False,
            data=data,
            error="NON_ZERO_EXIT",
            message=f"命令执行结束，返回码为 {result.exit_code}",
        )

    def _resolve_profile(self, exec_ctx: dict) -> Optional["CoreProfile"]:
        """从 __execution_context__ 推断 profile（用于安全校验）。"""
        from agent_core.kernel_interface.profile import CoreProfile

        profile_mode = (exec_ctx.get("profile_mode") or "full").lower()
        allow_dangerous = bool(exec_ctx.get("allow_dangerous_commands", False))

        return CoreProfile(
            mode=profile_mode if profile_mode in {"full", "sub", "background"} else "full",
            allow_dangerous_commands=allow_dangerous,
        )
