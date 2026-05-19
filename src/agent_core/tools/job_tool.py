"""
JobTool -- 后台独立进程工具集。

为长命令（安装、下载、编译、训练等）提供独立的执行环境，
不阻塞 Agent 主会话，不影响 persistent bash shell 的状态。

参考 Claude Code Bash tool 的 run_in_background 模式与 MCP Tasks 协议。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Optional

from agent_core.job_manager import JobStatus, get_job_manager
from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult

if TYPE_CHECKING:
    pass


class StartJobTool(BaseTool):
    """启动一个后台独立进程。"""

    def __init__(self, workspace_root: Optional[str] = None) -> None:
        self._manager = get_job_manager(workspace_root)

    @property
    def name(self) -> str:
        return "start_job"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""启动一个后台独立进程执行命令，不阻塞当前会话。

适用于安装依赖、下载数据、编译项目、模型训练等可能耗时较长的操作。
进程拥有独立的 process group，超时或手动停止时不会影响主 bash shell。

当你想要：
- 安装大量依赖（pip install、npm install 等）
- 下载大文件或数据集（wget、curl、git clone 等）
- 编译/构建项目（make、cargo build、npm run build 等）
- 运行训练脚本、长测试套件等
- 启动需要持续运行的后台服务

注意事项：
- 后台进程 stdout/stderr 被重定向到日志文件，可通过 job_tail 读取
- 超时后进程会被终止，但 bash shell 状态不受影响
- 进程工作目录和环境变量在启动时固定，后续 cd/export 不会同步到后台进程""",
            parameters=[
                ToolParameter(
                    name="command",
                    type="string",
                    description="要执行的 shell 命令",
                    required=True,
                ),
                ToolParameter(
                    name="cwd",
                    type="string",
                    description="工作目录（默认使用当前工作区根目录）",
                    required=False,
                ),
                ToolParameter(
                    name="timeout",
                    type="number",
                    description="超时时间（秒），0 或 null 表示不限制",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "后台安装依赖",
                    "params": {"command": "pip install torch", "timeout": 300},
                },
                {
                    "description": "后台下载数据集",
                    "params": {"command": "wget https://example.com/data.tar.gz", "timeout": 600},
                },
            ],
            usage_notes=[
                "后台进程独立运行，不影响主 bash shell 的状态",
                "使用 job_status 查询进度，job_tail 读取日志",
                "超时后进程会被终止，但 shell 状态不丢失",
            ],
            tags=["后台", "job", "异步", "长任务"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        command = str(kwargs.get("command", "")).strip()
        if not command:
            return ToolResult(
                success=False,
                error="MISSING_COMMAND",
                message="缺少必需参数: command",
            )

        cwd = kwargs.get("cwd")
        timeout = kwargs.get("timeout")
        if timeout is not None:
            try:
                timeout = float(timeout)
                if timeout <= 0:
                    timeout = None
            except (TypeError, ValueError):
                return ToolResult(
                    success=False,
                    error="INVALID_TIMEOUT",
                    message="timeout 必须是正数（秒）",
                )

        handle = await self._manager.start_job(
            command,
            cwd=cwd,
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


class JobStatusTool(BaseTool):
    """查询后台进程状态。"""

    def __init__(self, workspace_root: Optional[str] = None) -> None:
        self._manager = get_job_manager(workspace_root)

    @property
    def name(self) -> str:
        return "job_status"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="查询一个或多个后台进程的状态。",
            parameters=[
                ToolParameter(
                    name="job_id",
                    type="string",
                    description="任务 ID（start_job 返回的 job_id）",
                    required=True,
                ),
            ],
            tags=["后台", "job", "查询"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        job_id = str(kwargs.get("job_id", "")).strip()
        if not job_id:
            return ToolResult(
                success=False,
                error="MISSING_JOB_ID",
                message="缺少必需参数: job_id",
            )

        handle = await self._manager.job_status(job_id)
        if handle is None:
            return ToolResult(
                success=False,
                error="JOB_NOT_FOUND",
                message=f"未找到任务: {job_id}",
            )

        data = {
            "job_id": handle.job_id,
            "status": handle.status,
            "command": handle.command,
            "cwd": handle.cwd,
            "pid": handle.pid,
            "exit_code": handle.exit_code,
            "timed_out": handle.timed_out,
            "duration_seconds": round(handle.duration_seconds, 2),
            "log_path": str(handle.log_path),
        }

        if handle.status == JobStatus.RUNNING:
            return ToolResult(
                success=True,
                data=data,
                message=f"任务正在运行（已运行 {handle.duration_seconds:.1f}s）",
            )
        elif handle.status == JobStatus.FINISHED:
            return ToolResult(
                success=True,
                data=data,
                message=f"任务已完成，耗时 {handle.duration_seconds:.1f}s",
            )
        elif handle.status == JobStatus.TIMED_OUT:
            return ToolResult(
                success=False,
                data=data,
                error="JOB_TIMED_OUT",
                message=f"任务已超时（运行了 {handle.duration_seconds:.1f}s）",
            )
        elif handle.status == JobStatus.CANCELLED:
            return ToolResult(
                success=False,
                data=data,
                error="JOB_CANCELLED",
                message="任务已被取消",
            )
        else:
            return ToolResult(
                success=False,
                data=data,
                error="JOB_FAILED",
                message=f"任务失败，返回码 {handle.exit_code}",
            )


class JobTailTool(BaseTool):
    """读取后台进程日志尾部。"""

    def __init__(self, workspace_root: Optional[str] = None) -> None:
        self._manager = get_job_manager(workspace_root)

    @property
    def name(self) -> str:
        return "job_tail"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""读取后台进程的日志文件。

默认返回日志尾部最近 200 行；若任务刚开始，还会额外返回开头 50 行帮助定位。
支持 offset 参数实现续读（从上次读取位置继续）。""",
            parameters=[
                ToolParameter(
                    name="job_id",
                    type="string",
                    description="任务 ID",
                    required=True,
                ),
                ToolParameter(
                    name="lines",
                    type="number",
                    description="读取尾部行数（默认 200）",
                    required=False,
                    default=200,
                ),
                ToolParameter(
                    name="offset",
                    type="number",
                    description="从第几行开始读取（默认 0，从头开始；返回结果中会给出下次续读的 offset）",
                    required=False,
                    default=0,
                ),
            ],
            tags=["后台", "job", "日志"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        job_id = str(kwargs.get("job_id", "")).strip()
        if not job_id:
            return ToolResult(
                success=False,
                error="MISSING_JOB_ID",
                message="缺少必需参数: job_id",
            )

        lines = kwargs.get("lines", 200)
        offset = kwargs.get("offset", 0)
        try:
            lines = int(lines)
            offset = int(offset)
        except (TypeError, ValueError):
            return ToolResult(
                success=False,
                error="INVALID_PARAM",
                message="lines 和 offset 必须是整数",
            )

        result = await self._manager.job_tail(
            job_id,
            lines=max(1, lines),
            offset=max(0, offset),
        )
        if result is None:
            return ToolResult(
                success=False,
                error="JOB_NOT_FOUND",
                message=f"未找到任务: {job_id}",
            )

        head = result.get("head_lines", [])
        tail = result.get("tail_lines", [])
        total = result.get("total_lines", 0)
        next_offset = result.get("offset", 0)

        # 构建返回消息
        parts = []
        if head:
            parts.append("=== 开头 ===")
            parts.extend(head)
        if tail:
            parts.append("=== 尾部 ===")
            parts.extend(tail)

        message = f"日志共 {total} 行，本次读取 {len(head) + len(tail)} 行"
        if next_offset < total:
            message += f"，剩余 {total - next_offset} 行未读（offset={next_offset}）"

        return ToolResult(
            success=True,
            data={
                "job_id": job_id,
                "status": result.get("status"),
                "total_lines": total,
                "read_lines": len(head) + len(tail),
                "offset": next_offset,
                "log_path": result.get("log_path"),
                "head_lines": head,
                "tail_lines": tail,
            },
            message=message,
        )


class StopJobTool(BaseTool):
    """终止一个后台进程。"""

    def __init__(self, workspace_root: Optional[str] = None) -> None:
        self._manager = get_job_manager(workspace_root)

    @property
    def name(self) -> str:
        return "stop_job"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="终止一个正在运行的后台进程。",
            parameters=[
                ToolParameter(
                    name="job_id",
                    type="string",
                    description="任务 ID",
                    required=True,
                ),
                ToolParameter(
                    name="signal",
                    type="string",
                    description="发送的信号（默认 SIGTERM）",
                    required=False,
                    default="SIGTERM",
                ),
            ],
            tags=["后台", "job", "终止"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        job_id = str(kwargs.get("job_id", "")).strip()
        if not job_id:
            return ToolResult(
                success=False,
                error="MISSING_JOB_ID",
                message="缺少必需参数: job_id",
            )

        signal_name = str(kwargs.get("signal", "SIGTERM")).strip()
        ok = await self._manager.stop_job(job_id, signal_name=signal_name)
        if not ok:
            return ToolResult(
                success=False,
                error="STOP_FAILED",
                message=f"终止任务失败（任务可能不存在或已结束）: {job_id}",
            )

        return ToolResult(
            success=True,
            message=f"任务已终止: {job_id}",
        )
