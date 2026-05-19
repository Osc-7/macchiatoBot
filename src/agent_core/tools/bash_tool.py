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

from agent_core.permissions.broker import PathGrant, PermissionBroker, PermissionRequest
from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult

if TYPE_CHECKING:
    from agent_core.bash_runtime import BashRuntime
    from agent_core.bash_security import BashSecurity, SecurityVerdict
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
- 危险操作（rm -rf、chmod -R、sudo 等）或需要写入工作区外路径时，bash 会自动向人类申请权限；
  人类批准后同一次工具调用会继续执行原始命令并返回结果
- 超时后当前命令会被终止，bash 会话会自动重启并恢复之前的工作目录与环境变量
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
                    name="background",
                    type="boolean",
                    description="设为 true 时在独立后台进程中执行命令（不阻塞当前会话，不影响 bash shell 状态）。适用于安装、下载、编译、训练等长任务",
                    required=False,
                    default=False,
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
                    "description": "后台安装依赖（不阻塞会话）",
                    "params": {"command": "pip install torch", "background": True, "timeout": 300},
                },
                {
                    "description": "重启 bash 会话",
                    "params": {"restart": True},
                },
            ],
            usage_notes=[
                "这是持久化 bash 会话：cd、export 等在后续命令中生效",
                "危险命令和工作区外写入会自动申请人类批准，批准后继续执行同一条命令",
                "超时后当前命令会被终止，bash 会话会自动重启并恢复之前的工作目录与环境变量",
                "使用 restart=true 可手动重置会话（清除所有状态）",
                "长任务（安装依赖、下载大文件、编译、训练等）建议用 background=true 走独立后台进程，避免阻塞会话",
            ],
            tags=["命令", "终端", "bash", "执行"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        exec_ctx = kwargs.pop("__execution_context__", None) or {}

        restart = kwargs.get("restart", False)
        if restart:
            session_id = str(exec_ctx.get("session_id") or "").strip()
            if session_id:
                from agent_core.remote.workspace_state import (
                    get_remote_workspace_state,
                )
                from agent_core.remote.worker_registry import (
                    get_remote_worker_registry,
                )

                remote_state = get_remote_workspace_state(session_id)
                if remote_state is not None:
                    try:
                        res = await get_remote_worker_registry().reset_remote_shell(
                            login=remote_state.login,
                            session_id=session_id,
                        )
                    except Exception as exc:
                        return ToolResult(
                            success=False,
                            error="REMOTE_SHELL_RESET_FAILED",
                            message=f"远程 bash 重启失败: {exc}",
                        )
                    if not res.success:
                        return ToolResult(
                            success=False,
                            error="REMOTE_SHELL_RESET_FAILED",
                            message=res.error or res.message or "远程 bash 重启失败",
                        )
                    return ToolResult(
                        success=True,
                        message="远程 bash 会话已重启，环境变量与工作目录已清除",
                    )
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

        # ── background mode：长命令走独立 job，不阻塞 shell ─────────
        if kwargs.get("background"):
            from agent_core.job_manager import get_job_manager

            # 远程 worker 场景暂不支持 background（后续扩展）
            session_id = str(exec_ctx.get("session_id") or "").strip()
            if session_id:
                from agent_core.remote.workspace_state import get_remote_workspace_state

                remote_state = get_remote_workspace_state(session_id)
                if remote_state is not None:
                    return ToolResult(
                        success=False,
                        error="NOT_SUPPORTED",
                        message="远程模式暂不支持 background 参数，请使用本地 bash 工具",
                    )

            manager = get_job_manager()
            timeout = kwargs.get("timeout")
            if timeout is not None:
                try:
                    timeout = float(timeout)
                except (TypeError, ValueError):
                    timeout = None

            handle = await manager.start_job(
                command,
                cwd=str(kwargs.get("cwd", ".")),
                timeout_seconds=timeout,
            )
            return ToolResult(
                success=True,
                data={
                    "job_id": handle.job_id,
                    "pid": handle.pid,
                    "log_path": str(handle.log_path),
                    "status": handle.status,
                    "command": handle.command,
                },
                message=f"后台任务已启动: {handle.job_id} (pid={handle.pid})",
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

        remote_result = await self._try_execute_remote(
            command=command,
            timeout=timeout,
            confirmed=False,
            profile=self._resolve_profile(exec_ctx),
            exec_ctx=exec_ctx,
        )
        if remote_result is not None:
            return remote_result

        # 忽略模型可能仍传入的 confirm（不再具有效力；批准仅能通过人类审批）
        kwargs.pop("confirm", None)

        from agent_core.permissions.bash_danger_approvals import (
            consume_bash_danger_grant,
        )

        perm_id = kwargs.get("permission_id")
        perm_str = str(perm_id).strip() if perm_id is not None else ""
        confirmed = bool(perm_str) and consume_bash_danger_grant(perm_str, command)

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

        if verdict.needs_confirmation:
            perm_result = await self._request_bash_permission(
                command=command,
                verdict=verdict,
                exec_ctx=exec_ctx,
                cwd=await self._current_cwd(),
            )
            if perm_result is not None:
                return perm_result
            if self._security and getattr(self._security, "_workspace_jail_root", None):
                self._security.refresh_write_roots_from_config(
                    str(exec_ctx.get("source") or "cli"),
                    str(exec_ctx.get("user_id") or "root"),
                )
            verdict = self._security.check(
                command,
                profile=profile,
                confirmed=True,
            )

        if verdict.denied or verdict.needs_confirmation:
            return ToolResult(
                success=False,
                error=verdict.error_code or "PERMISSION_DENIED",
                message=verdict.reason or "权限检查未通过",
                data={
                    "denied_command": command,
                    "risk_reasons": list(verdict.risk_reasons),
                    "path_grants": list(verdict.path_grants),
                },
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
            mode=(
                profile_mode
                if profile_mode in {"full", "sub", "background"}
                else "full"
            ),
            allow_dangerous_commands=allow_dangerous,
        )

    async def _current_cwd(self) -> Optional[str]:
        try:
            result = await self._bash.execute("pwd", timeout=2.0, output_limit=2000)
        except Exception:
            return None
        if result.exit_code != 0:
            return None
        return result.stdout.strip() or None

    async def _request_bash_permission(
        self,
        *,
        command: str,
        verdict: "SecurityVerdict",
        exec_ctx: dict,
        cwd: Optional[str],
    ) -> Optional[ToolResult]:
        grants = [
            grant
            for raw in verdict.path_grants
            if (grant := PathGrant.from_payload(raw)) is not None
        ]
        risks = list(verdict.risk_reasons)
        if not risks and verdict.error_code == "WORKSPACE_WRITE_DENIED":
            risks = ["写入工作区外路径"]
        summary_parts = ["执行 bash 命令需要批准"]
        if risks:
            summary_parts.append("风险: " + "；".join(risks))
        if grants:
            summary_parts.append("路径: " + ", ".join(g.path_prefix for g in grants))
        broker = PermissionBroker()
        res = await broker.request(
            PermissionRequest(
                tool_name="bash",
                kind=(
                    "bash_dangerous_command"
                    if verdict.risk_reasons
                    else "bash_write_outside_workspace"
                ),
                summary="；".join(summary_parts),
                details={"command": command},
                command=command,
                cwd=cwd,
                risk_reasons=risks,
                path_grants=grants,
                auto_execute_after_approval=True,
                exec_ctx=dict(exec_ctx),
            )
        )
        if res.allowed:
            return None
        return self._permission_failure_result(res.error, res.message, command, res)

    @staticmethod
    def _permission_failure_result(
        error: Optional[str],
        message: str,
        command: str,
        broker_result: object,
    ) -> ToolResult:
        return ToolResult(
            success=False,
            error=error or "PERMISSION_DENIED",
            message=message or "人类未批准该 bash 命令",
            data={
                "command": command,
                "permission_id": getattr(broker_result, "permission_id", None),
                "user_instruction": getattr(broker_result, "user_instruction", ""),
            },
        )

    async def _try_execute_remote(
        self,
        *,
        command: str,
        timeout: Optional[float],
        confirmed: bool,
        profile: Optional["CoreProfile"],
        exec_ctx: dict,
    ) -> Optional[ToolResult]:
        """Route bash to a connected remote worker when this session is remote."""
        session_id = str(exec_ctx.get("session_id") or "").strip()
        if not session_id:
            return None
        from agent_core.remote.workspace_state import get_remote_workspace_state

        remote_state = get_remote_workspace_state(session_id)
        if remote_state is None:
            return None

        from agent_core.config import get_config
        from agent_core.remote.worker_registry import get_remote_worker_registry

        cfg = get_config()
        remote_security = type(self._security)(
            restricted_whitelist=list(
                cfg.command_tools.subagent_command_whitelist or []
            ),
            allow_run_for_restricted=cfg.command_tools.allow_run_for_subagent,
        )
        verdict = remote_security.check(
            command,
            profile=profile,
            confirmed=confirmed,
        )
        if verdict.denied:
            return ToolResult(
                success=False,
                error=verdict.error_code,
                message=verdict.reason,
            )
        if verdict.needs_confirmation:
            perm_result = await self._request_remote_bash_permission(
                command=command,
                verdict=verdict,
                exec_ctx=exec_ctx,
                cwd=remote_state.workspace_mount,
            )
            if perm_result is not None:
                return perm_result
            verdict = remote_security.check(
                command,
                profile=profile,
                confirmed=True,
            )
            if verdict.denied or verdict.needs_confirmation:
                return ToolResult(
                    success=False,
                    error=verdict.error_code or "PERMISSION_DENIED",
                    message=verdict.reason or "远程 bash 权限检查未通过",
                )

        registry = get_remote_worker_registry()
        reopen_attempted = False
        reopen_succeeded = False
        try:
            result = await registry.execute_command(
                login=remote_state.login,
                session_id=session_id,
                command=command,
                timeout_seconds=timeout,
                output_limit=cfg.command_tools.default_output_limit,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error="REMOTE_WORKER_ERROR",
                message=f"远程 worker 执行失败: {exc}",
                data={"login": remote_state.login, "session_id": session_id},
            )
        if self._is_remote_session_not_open_result(result):
            reopen_attempted = True
            try:
                open_result = await registry.open_workspace(
                    login=remote_state.login,
                    session_id=session_id,
                    requested_path=(
                        remote_state.requested_path or remote_state.resolved_path or "~"
                    ),
                    profile=remote_state.profile,
                )
                reopen_succeeded = bool(open_result.success)
            except Exception:
                reopen_succeeded = False
            if reopen_succeeded:
                try:
                    result = await registry.execute_command(
                        login=remote_state.login,
                        session_id=session_id,
                        command=command,
                        timeout_seconds=timeout,
                        output_limit=cfg.command_tools.default_output_limit,
                    )
                except Exception as exc:
                    return ToolResult(
                        success=False,
                        error="REMOTE_WORKER_ERROR",
                        message=f"远程会话重连后执行失败: {exc}",
                        data={
                            "login": remote_state.login,
                            "session_id": session_id,
                            "remote_reopen_attempted": True,
                            "remote_reopen_succeeded": True,
                        },
                    )

        data = {
            "command": result.command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.exit_code,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
            "remote": True,
            "remote_login": remote_state.login,
            "remote_cwd": result.cwd,
        }
        metadata = {"workspace_backend": "remote", "remote_login": remote_state.login}
        if reopen_attempted:
            metadata["remote_reopen_attempted"] = True
            metadata["remote_reopen_succeeded"] = reopen_succeeded
        if result.timed_out:
            return ToolResult(
                success=False,
                data=data,
                error="COMMAND_TIMEOUT",
                message="远程命令执行超时，远程 shell 会话已重启",
                metadata=metadata,
            )
        if result.exit_code == 0:
            return ToolResult(
                success=True,
                data=data,
                message="远程命令执行成功",
                metadata=metadata,
            )
        return ToolResult(
            success=False,
            data=data,
            error="NON_ZERO_EXIT",
            message=f"远程命令执行结束，返回码为 {result.exit_code}",
            metadata=metadata,
        )

    @staticmethod
    def _is_remote_session_not_open_result(result: object) -> bool:
        err = str(getattr(result, "stderr", "") or "").lower()
        return "remote session is not open" in err

    async def _request_remote_bash_permission(
        self,
        *,
        command: str,
        verdict: "SecurityVerdict",
        exec_ctx: dict,
        cwd: Optional[str],
    ) -> Optional[ToolResult]:
        broker = PermissionBroker()
        risks = list(verdict.risk_reasons) or ["远程危险 bash 命令"]
        res = await broker.request(
            PermissionRequest(
                tool_name="bash",
                kind="bash_dangerous_command",
                summary="执行远程 bash 命令需要批准；风险: " + "；".join(risks),
                details={"command": command, "remote": True},
                command=command,
                cwd=cwd,
                risk_reasons=risks,
                timeout_seconds=300.0,
                auto_execute_after_approval=True,
                exec_ctx=dict(exec_ctx),
            )
        )
        if res.allowed:
            return None
        return self._permission_failure_result(res.error, res.message, command, res)
