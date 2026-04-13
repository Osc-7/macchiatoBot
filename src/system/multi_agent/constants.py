"""
Multi-Agent 共享常量。

约定：
- `KernelRequest.frontend_id` 在多数场景表示「前端/渠道」；P2P 工具复用该字段存放
  **投递语义标签**，不得与 CoreProfile 的 memory source 混用（见 KernelScheduler）。
"""

# Agent 间 P2P：KernelRequest 上的投递标签（非 Core 类型名、非记忆命名空间）
P2P_REQUEST_FRONTEND_TAG: str = "agent_msg"

# KernelRequest.metadata 中与 AgentMessage 信封绑定的键
METADATA_KEY_AGENT_MESSAGE: str = "_agent_message"
