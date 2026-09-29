"""switch_workspace：把当前会话的工具执行切到已连接的 worker，或切回 daemon。"""

from __future__ import annotations

from typing import Any, Optional

from agent_core.remote.workspace_switch import (
    REMOTE_PROFILES,
    current_kernel_agent,
    perform_workspace_switch,
)
from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult


class SwitchWorkspaceTool(BaseTool):
    """会话级工作区切换。准入检查在 Kernel 调用时执行。"""

    @property
    def name(self) -> str:
        return "switch_workspace"

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="""把当前会话的 bash / 文件工具 / load_skill 切到另一台已连接的机器，或切回 daemon。

当任务需要的文件或工具链在工作服务器、个人电脑等远程 worker 上，而当前工具还在 daemon 上时使用。
做完远程操作、需要回到 daemon 磁盘时，把 target 设为 daemon。

省略 path 时使用该 worker 启动 macchiato-remote 时的目录。权限档位默认 dev。
Kernel 会拒绝未登录、没有活跃连接、非法权限档位，以及当前 Core 无权切换的请求。水源、定时任务等会话不能使用本工具。不要猜测 login，只使用系统提示「运行环境」里列出的机器。
一次会话只绑定一台机器。本工具请单独调用，切换成功后再在下一轮使用 bash 和文件工具。""",
            parameters=[
                ToolParameter(
                    name="target",
                    type="string",
                    description=(
                        "要切到的 worker login，或 daemon（也接受 cloud / local）以回到 daemon 所在机器。"
                    ),
                    required=True,
                ),
                ToolParameter(
                    name="path",
                    type="string",
                    description=(
                        "远程授权目录。省略时用该 worker 的默认工作区"
                        "（启动 macchiato-remote 时的目录）。切回 daemon 时忽略。"
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="profile",
                    type="string",
                    description=(
                        "远程权限档位，默认 dev。可选 "
                        + "、".join(REMOTE_PROFILES)
                        + "。host-user / host-admin 只在用户明确要求更高权限时使用。"
                    ),
                    required=False,
                    enum=list(REMOTE_PROFILES),
                ),
                ToolParameter(
                    name="ttl_seconds",
                    type="integer",
                    description="绑定租约秒数。省略则沿用远程工作区默认租约。",
                    required=False,
                ),
            ],
            examples=[
                {
                    "description": "切到已连接的工作服务器，使用它的默认工作区",
                    "params": {"target": "<login>"},
                },
                {
                    "description": "切到某台机器上的指定目录，权限保持 dev",
                    "params": {
                        "target": "<login>",
                        "path": "~/Project",
                        "profile": "dev",
                    },
                },
                {
                    "description": "从远程机器回到 daemon",
                    "params": {"target": "daemon"},
                },
            ],
            usage_notes=[
                "只切换运行环境列表中的 login；未登录或 worker 离线会被 Kernel 拒绝。",
                "默认路径是该 worker 进程的启动目录，不是 daemon 的目录。",
                "切换不搬运文件。记忆仍在 daemon。",
                "host-user 与 host-admin 不是默认档位。",
                "与 bash、read_file、write_file 不要放在同一轮并行调用。",
            ],
            tags=["workspace", "remote", "daemon"],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        exec_ctx = kwargs.get("__execution_context__")
        if not isinstance(exec_ctx, dict):
            exec_ctx = {}
        session_id = str(exec_ctx.get("session_id") or "").strip()
        agent = current_kernel_agent()
        if not session_id:
            session_id = str(getattr(agent, "_session_id", "") or "")
        ttl = _optional_int(kwargs.get("ttl_seconds"))
        return await perform_workspace_switch(
            session_id=session_id,
            target=str(kwargs.get("target") or ""),
            path=str(kwargs.get("path") or ""),
            profile=str(kwargs.get("profile") or ""),
            ttl_seconds=ttl,
            agent=agent,
        )


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
