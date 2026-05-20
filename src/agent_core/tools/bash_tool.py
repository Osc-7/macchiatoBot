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
            description="""在持久化 bash 会话中执行命令，或管理后台任务。

这是一个持久化的 bash 会话：环境变量、工作目录、命令历史在整个对话期间保持。
前一条命令创建的文件、设置的变量，在后续命令中仍然可用。

当你想要：
- 查看目录/文件信息（ls、pwd、find 等）
- 运行脚本或测试命令（pytest、python script.py 等）
- 查询 Git 状态、构建状态等开发信息
- 执行多步工作流（cd 到目录 → 安装 → 构建 → 测试）

**长任务管理**：
- 使用 background=true 在独立后台进程执行命令，不阻塞当前会话
- 安装依赖、下载大文件、编译、训练等用 background
- 后台任务启动后可使用 job_status/job_tail/job_stop 管理

注意事项：
- 危险操作（rm -rf、chmod -R、sudo 等）或需要写入工作区外路径时，bash 会自动向人类申请权限；
  人类批准后同一次工具调用会继续执行原始命令并返回结果
- 同步命令超时后当前命令会被终止，bash 会话会自动重启并尝试恢复工作目录与环境变量
- 使用 restart=true 可手动重启 bash 会话（清除所有状态；若启用 snapshot_enabled 则 evict 时也可恢复）""",
            parameters=[
                ToolParameter(
                    name="command",
                    type="string",
                    description="要执行的 shell 命令",
                    required=False,
                ),
                ToolParameter(
                    name="background",
                    type="boolean",
                    description="设为 true 时在独立后台进程中执行命令（不阻塞当前会话，不影响 bash shell 状态）。适用于安装、下载、编译、训练等长任务",
                    required=False,
                    default=False,
                ),
                ToolParameter(
                    name="job_status",
                    type="string",
                    description="查询后台任务状态（传入 job_id，返回状态/exit_code/用时）",
                    required=False,
                ),
                ToolParameter(
                    name="job_tail",
                    type="string",
                    description="读取后台任务日志（传入 job_id），配合 lines/offset 控制输出",
                    required=False,
                ),
                ToolParameter(
                    name="job_stop",
                    type="string",
                    description="终止后台任务（传入 job_id），配合 signal 指定信号",
                    required=False,
                ),
                ToolParameter(
                    name="lines",
                    type="number",
                    description="job_tail 时读取的尾部行数（默认 200）",
                    required=False,
                    default=200,
                ),
                ToolParameter(
                    name="offset",
                    type="number",
                    description="job_tail 时从第几行开始读取（默认 0，返回结果中会给出下次续读的 offset）",
                    required=False,
                    default=0,
                ),
                ToolParameter(
                    name="signal",
                    type="string",
                    description="job_stop 时发送的信号（默认 SIGTERM）",
                    required=False,
                    default="SIGTERM",
                ),
                ToolParameter(
                    name="timeout",
                    type="number",
                    description="同步命令的超时时间（秒），超时后命令会被终止且 bash 会话自动重启；background 模式时则为后台任务超时",
                    required=False,
                ),
                ToolParameter(
                    name="restart",
                    type="boolean",
                    description="设为 true 时重启 bash 会话（清除所有环境变量和工作目录状态）",
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
                    "description": "查询后台任务状态",
                    "params": {"job_status": "job_abc123"},
                },
                {
                    "description": "读取后台任务日志尾部",
                    "params": {"job_tail": "job_abc123", "lines": 100, "offset": 0},
                },
                {
                    "description": "终止后台任务",
                    "params": {"job_stop": "job_abc123"},
                },
                {
                    "description": "重启 bash 会话",
                    "params": {"restart": True},
                },
            ],
            usage_notes=[
                "这是持久化 bash 会话：cd、export 等在后续命令中生效",
                "危险命令和工作区外写入会自动申请人类批准，批准后继续执行同一条命令",
                "同步命令超时后会重启 bash 并尝试恢复 cwd/env；长任务请用 background=true",
                "使用 restart=true 可手动重置会话（清除所有状态）",
                "长任务（安装依赖、下载大文件、编译、训练等）建议用 background=true 走独立后台进程，避免阻塞会话",
                "后台任务日志写入工作区 .macchiato/jobs/ 目录，可用 job_tail 读取",
            ],
            tags=["命令", "终端", "bash", "执行", "后台job"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        exec_ctx = kwargs.pop("__execution_context__", None) or {}

        param_err = self._validate_params(kwargs)
        if param_err is not None:
            return param_err

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

        # ── job management：查状态 / 读日志 / 终止 ────────────────
        job_status_id = str(kwargs.get("job_status") or "").strip()
        job_tail_id = str(kwargs.get("job_tail") or "").strip()
        job_stop_id = str(kwargs.get("job_stop") or "").strip()
        if job_status_id or job_tail_id or job_stop_id:
            return await self._handle_job_action(
                job_status_id=job_status_id,
                job_tail_id=job_tail_id,
                job_stop_id=job_stop_id,
                lines=kwargs.get("lines", 200),
                offset=kwargs.get("offset", 0),
                signal_name=kwargs.get("signal", "SIGTERM"),
            )

        command = str(kwargs.get("command", "")).strip()
        if not command:
            return ToolResult(
                success=False,
                error="MISSING_COMMAND",
                message="缺少必需参数: command（或使用 restart=true / job_* 管理后台任务）",
            )

        timeout, timeout_err = self._parse_timeout(kwargs.get("timeout"))
        if timeout_err is not None:
            return timeout_err

        if kwargs.get("background"):
            return await self._execute_background(
                command=command,
                timeout=timeout,
                exec_ctx=exec_ctx,
                kwargs=kwargs,
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

        security_result = await self._ensure_command_allowed(
            command=command,
            exec_ctx=exec_ctx,
            kwargs=kwargs,
        )
        if security_result is not None:
            return security_result

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
                message="命令执行超时，进程已终止；bash 会话已重启并尝试恢复工作目录与环境变量",
            )

        if result.exit_code == 0:
            return ToolResult(success=True, data=data, message="命令执行成功")

        return ToolResult(
            success=False,
            data=data,
            error="NON_ZERO_EXIT",
            message=f"命令执行结束，返回码为 {result.exit_code}",
        )

    # ── 参数校验与安全（同步 / background 共用）────────────────

    @staticmethod
    def _validate_params(kwargs: dict) -> Optional[ToolResult]:
        restart = bool(kwargs.get("restart"))
        has_command = bool(str(kwargs.get("command") or "").strip())
        background = bool(kwargs.get("background"))
        has_job = any(
            str(kwargs.get(k) or "").strip()
            for k in ("job_status", "job_tail", "job_stop")
        )

        if restart and (has_command or background or has_job):
            return ToolResult(
                success=False,
                error="CONFLICTING_PARAMS",
                message="restart 不能与 command、background 或 job_* 同时使用",
            )
        if has_job and (has_command or background):
            return ToolResult(
                success=False,
                error="CONFLICTING_PARAMS",
                message="job_status/job_tail/job_stop 不能与 command 或 background 同时使用",
            )
        if background and not has_command:
            return ToolResult(
                success=False,
                error="MISSING_COMMAND",
                message="background=true 时必须提供 command",
            )
        return None

    @staticmethod
    def _parse_timeout(raw: object) -> tuple[Optional[float], Optional[ToolResult]]:
        if raw is None:
            return None, None
        try:
            return float(raw), None
        except (TypeError, ValueError):
            return None, ToolResult(
                success=False,
                error="INVALID_TIMEOUT",
                message="timeout 必须是数字（秒）",
            )

    async def _ensure_command_allowed(
        self,
        *,
        command: str,
        exec_ctx: dict,
        kwargs: dict,
    ) -> Optional[ToolResult]:
        """BashSecurity + 人类审批。返回 ToolResult 表示中止；None 表示可执行。"""
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
        return None

    async def _execute_background(
        self,
        *,
        command: str,
        timeout: Optional[float],
        exec_ctx: dict,
        kwargs: dict,
    ) -> ToolResult:
        import os
        from pathlib import Path

        from agent_core.job_manager import get_job_manager

        session_id = str(exec_ctx.get("session_id") or "").strip()
        if session_id:
            from agent_core.remote.workspace_state import get_remote_workspace_state

            if get_remote_workspace_state(session_id) is not None:
                return ToolResult(
                    success=False,
                    error="NOT_SUPPORTED",
                    message="远程模式暂不支持 background 参数，请使用同步 command",
                )

        security_result = await self._ensure_command_allowed(
            command=command,
            exec_ctx=exec_ctx,
            kwargs=kwargs,
        )
        if security_result is not None:
            return security_result

        ws_root = str(Path(self._bash._config.base_dir).resolve())
        if self._bash._config.subprocess_env is not None:
            base_env = dict(self._bash._config.subprocess_env)
        else:
            base_env = dict(os.environ)

        session = await self._bash.capture_session()
        if session is not None:
            job_cwd = session.cwd
            job_env = {**base_env, **session.env}
        else:
            job_cwd = await self._current_cwd() or ws_root
            job_env = base_env

        manager = get_job_manager(workspace_root=ws_root)
        handle = await manager.start_job(
            command,
            cwd=job_cwd,
            env=job_env,
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
                "cwd": handle.cwd,
            },
            message=f"后台任务已启动: {handle.job_id} (pid={handle.pid})",
        )

    # ── job management helpers ───────────────────────────────

    def _job_manager(self) -> "JobManager":
        from agent_core.job_manager import get_job_manager
        from pathlib import Path

        ws_root = str(Path(self._bash._config.base_dir).resolve())
        return get_job_manager(workspace_root=ws_root)

    async def _handle_job_action(
        self,
        *,
        job_status_id: str = "",
        job_tail_id: str = "",
        job_stop_id: str = "",
        lines: int = 200,
        offset: int = 0,
        signal_name: str = "SIGTERM",
    ) -> ToolResult:
        manager = self._job_manager()

        if job_status_id:
            handle = await manager.job_status(job_status_id)
            if handle is None:
                return ToolResult(
                    success=False,
                    error="JOB_NOT_FOUND",
                    message=f"未找到后台任务: {job_status_id}",
                )
            data = {
                "job_id": handle.job_id,
                "status": handle.status,
                "command": handle.command,
                "pid": handle.pid,
                "exit_code": handle.exit_code,
                "timed_out": handle.timed_out,
                "duration_seconds": round(handle.duration_seconds, 2),
                "log_path": str(handle.log_path),
            }
            if handle.status == "running":
                return ToolResult(success=True, data=data, message=f"任务正在运行（已运行 {handle.duration_seconds:.1f}s）")
            elif handle.status == "finished":
                return ToolResult(success=True, data=data, message=f"任务已完成，耗时 {handle.duration_seconds:.1f}s，返回码 {handle.exit_code}")
            elif handle.status == "timed_out":
                return ToolResult(success=False, data=data, error="JOB_TIMED_OUT", message=f"任务已超时（运行了 {handle.duration_seconds:.1f}s）")
            elif handle.status == "cancelled":
                return ToolResult(success=False, data=data, error="JOB_CANCELLED", message="任务已被取消")
            else:
                return ToolResult(success=False, data=data, error="JOB_FAILED", message=f"任务失败，返回码 {handle.exit_code}")

        if job_tail_id:
            result = await manager.job_tail(job_tail_id, lines=max(1, int(lines)), offset=max(0, int(offset)))
            if result is None:
                return ToolResult(success=False, error="JOB_NOT_FOUND", message=f"未找到后台任务: {job_tail_id}")
            data = {
                "job_id": job_tail_id,
                "status": result.get("status"),
                "total_lines": result.get("total_lines", 0),
                "read_lines": len(result.get("head_lines", [])) + len(result.get("tail_lines", [])),
                "offset": result.get("offset", 0),
                "log_path": result.get("log_path"),
                "head_lines": result.get("head_lines", []),
                "tail_lines": result.get("tail_lines", []),
            }
            msg = f"日志共 {data['total_lines']} 行"
            remaining = data["total_lines"] - data["offset"]
            if remaining > 0:
                msg += f"，剩余 {remaining} 行未读（offset={data['offset']}）"
            return ToolResult(success=True, data=data, message=msg)

        if job_stop_id:
            ok = await manager.stop_job(job_stop_id, signal_name=signal_name)
            if not ok:
                return ToolResult(success=False, error="STOP_FAILED", message=f"终止任务失败（任务可能不存在或已结束）: {job_stop_id}")
            return ToolResult(success=True, message=f"后台任务已终止: {job_stop_id}")

        return ToolResult(success=False, error="NO_ACTION", message="未指定 job 操作")

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
