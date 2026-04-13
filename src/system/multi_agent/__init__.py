"""
Multi-Agent 运行时 — 与 Kernel / CorePool 同进程的原生多会话协作层。

本包不实现「另一个 Agent 框架」，而是把仓库里已有的能力**收束命名与边界**：

- **会话即进程 (session_id → AgentCore)**：每个对等体独占一条对话链与 CoreProfile；
  调度器按 session 并行、同 session 串行（见 `system.kernel.scheduler`）。
- **父子委托**：`create_subagent` / `create_parallel_subagents` → `CorePool.register_sub`
  → 受限 `CoreProfile.mode=sub` → 终态经 `inject_turn` 通知父（非 P2P 汇报路径）。
- **对等 P2P**：`send_message_to_agent` / `reply_to_message` → `KernelScheduler.inject_turn`
  + `metadata[METADATA_KEY_AGENT_MESSAGE]`（`AgentMessage` 信封）+ 可选阻塞 Future。
- **进程表（只读投影）**：`registry.build_full_process_table(CorePool)` 遍历
  `list_entries(include_zombies=True)`；`list_agents` 工具按 `scope` 过滤（见 `filter_agent_rows`）。
  子任务 **自然停轮** 后 `on_sub_complete`，父可续 P2P 或 `reap`；可选配置
  `agent.subagent_zombie_ttl_seconds` 由调度器兜底 `reap_zombie`。

与外部 A2A / 多机编排对接时，建议以 `AgentMessage` 字段为稳定映射面（见
`agent_core.kernel_interface.action.AgentMessage` 文档字符串）。

导出：
"""

from .constants import METADATA_KEY_AGENT_MESSAGE, P2P_REQUEST_FRONTEND_TAG
from .registry import (
    build_full_process_table,
    filter_agent_rows,
    memory_namespace_key,
    project_entry_row,
)

__all__ = [
    "METADATA_KEY_AGENT_MESSAGE",
    "P2P_REQUEST_FRONTEND_TAG",
    "build_full_process_table",
    "filter_agent_rows",
    "memory_namespace_key",
    "project_entry_row",
]
