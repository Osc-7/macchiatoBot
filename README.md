# machiattoBot

中文 | [English](README_EN.md)


一个基于 LLM 的人工智能助手，采用 **Tool-driven + Kernel 调度** 架构，强调可控、可扩展与长期运行稳定性。

machiattoBot 将 AgentCore 的“推理”与 Kernel 的“执行/权限/回收”分离：

- AgentCore 负责对话与决策
- Kernel 负责工具调用、上下文压缩与生命周期管理
- Scheduler 负责多会话并发与 TTL 回收

设计了**内核化架构**：把推理与执行分离，并将调度、权限与回收纳入统一的 Kernel/Scheduler 体系中。

- **Kernel 化架构**：推理与 IO 解耦，工具调用统一由 Kernel 执行
- **多会话并发 + TTL 回收**：适合常驻进程与多终端协作
- **自动化链路一体化**：定时任务、队列、IPC 一条链路贯通
- **工具系统可扩展**：统一注册与权限过滤，支持 MCP 工具接入
- **记忆与上下文策略**：工作记忆压缩、对话历史检索、长期记忆
- **多端接入**

## 快速开始

```bash
# 1) 初始化环境
source init.sh

# 2) 配置
cp config.example.yaml config.yaml
# 填写 llm.api_key / llm.model（或环境变量覆盖）

# 3) 启动 automation daemon（推荐）
python automation_daemon.py

# 4) 启动前端
python main.py # 启动 CLI
python feishu_ws_gateway.py # 启动飞书服务器

# 5) 单条命令模式
python main.py 明天下午3点开会

# 可选：覆盖默认用户/来源（默认 user_id=root, source=cli）
SCHEDULE_USER_ID=root SCHEDULE_SOURCE=cli python main.py
```

## 运行模式

### 1) 后台进程

```bash
python automation_daemon.py
```

### 2) 交互式 CLI

```bash
python main.py
```

Daemon 会执行：

- 从 `config.yaml` 同步自动化 job 定义（沿用现有调度链路）
- 调度器按规则入队，消费者执行队列任务
- 暴露本地 IPC（Unix Socket）给 CLI / 其他前端
- 在 automation 进程内统一执行 session expired 检查与切分（idle + 4am）

## CLI 会话命令

CLI 会话命令（通过 IPC 或本地模式）：

- `/session`：显示当前会话
- `/session list`：列出已加载会话
- `/session new [id]`：创建并切换新会话
- `/session switch <id>`：切换到已有会话

示例：

```bash
/session
/session new cli:work
/session list
/session switch cli:default
```

说明：

- 推荐通过 `automation_daemon.py` 运行，跨终端共享会话视图。
- 会话列表通过共享注册表跨终端可见（同一 `SCHEDULE_USER_ID` + `SCHEDULE_SOURCE`）。
- 记忆/对话历史默认按 `user_id` 命名空间隔离（默认 `root`）。
- CLI 不再本地执行过期切分；过期由 automation 常驻进程统一处理。

## 配置要点

主配置文件：`config.yaml`（参考 `config.example.yaml`）。

常用字段：


| 字段                            | 说明                     |
| ----------------------------- | ---------------------- |
| `llm.provider`                | `doubao` 或 `qwen`      |
| `llm.api_key`                 | LLM 密钥                 |
| `llm.model`                   | 模型名或端点 ID              |
| `time.timezone`               | 时区（默认 `Asia/Shanghai`） |
| `storage.data_dir`            | 本地数据目录                 |
| `memory.*`                    | 会话总结与记忆策略              |
| `automation.jobs`             | 自动化任务定义                |
| `mcp.enabled` / `mcp.servers` | MCP 客户端与远端工具           |


## 架构一览

```text
User/Frontend
   │
   ▼
Automation Core Gateway ── IPC ── CLI / Feishu / MCP
   │
   ▼
KernelScheduler ── OutputRouter ── Futures
   │
   ▼
AgentKernel ── ToolRegistry ── Tools (IO)
   │
   ▼
AgentCore (LLM 推理 + 决策)
```

## 项目结构

```text
src/
├── agent_core/    # AgentCore、Kernel 协议、工具与记忆
├── system/        # KernelScheduler、CorePool、automation/runtime
└── frontend/      # CLI、飞书、MCP 等多端接入
```

## 开发与测试

```bash
source init.sh
pytest tests/ -v
```

## MCP 本地入口

```bash
python mcp_server.py
```

如果要让 Agent 调用本地 MCP 工具，可在 `config.yaml` 配置 `mcp.servers`（stdio）。

---

开发规范见 [AGENTS.md](AGENTS.md)。
