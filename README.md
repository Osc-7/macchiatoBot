# Schedule Agent

基于 LLM 的工具驱动日程助手，支持自然语言日程管理、自动化任务队列和多会话 CLI。

## 当前能力

- 自然语言创建/查询/修改事件与任务
- 工具驱动主循环（LLM 决策，工具执行）
- 多模型接入：`doubao`、`qwen`
- 自动化链路：调度器 + 队列 + 常驻 Daemon（`automation_daemon.py`）
- Core 抽象层：`core/interfaces` + `core/adapters`
- CLI 多终端会话（推荐通过 automation IPC 连接常驻 Daemon）
- MCP 客户端接入外部工具（按配置自动注册）

## 快速开始

```bash
# 1) 初始化环境
source init.sh

# 2) 配置
cp config.example.yaml config.yaml
# 填写 llm.api_key / llm.model（或环境变量覆盖）

# 3) 启动 automation daemon（推荐）
python automation_daemon.py

# 4) 启动 CLI（会自动优先连接 daemon 的本地 IPC）
python main.py

# 5) 单条命令模式
python main.py 明天下午3点开会

# 可选：覆盖默认用户/来源（默认 user_id=root, source=cli）
SCHEDULE_USER_ID=root SCHEDULE_SOURCE=cli python main.py
```

## 运行模式

### 1) 交互式 CLI

```bash
python main.py
```

### 2) 后台自动化 Daemon（推荐）

```bash
python automation_daemon.py
```

Daemon 会执行：
- 从 `config.yaml` 同步自动化 job 定义（沿用现有调度链路）
- 调度器按规则入队，消费者执行队列任务
- 暴露本地 IPC（Unix Socket）给 CLI / 其他前端
- 在 automation 进程内统一执行 session expired 检查与切分（idle + 4am）

兼容模式：
- 仍可运行 `python agent_worker.py`（仅队列消费，不提供 IPC）。

## CLI 会话命令

CLI 会话命令（通过 IPC 或本地模式）：

- `session`：显示当前会话
- `session list`：列出已加载会话
- `session new [id]`：创建并切换新会话
- `session switch <id>`：切换到已有会话

示例：

```bash
session
session new cli:work
session list
session switch cli:default
```

说明：
- 推荐通过 `automation_daemon.py` 运行，跨终端共享会话视图。
- 会话列表通过共享注册表跨终端可见（同一 `SCHEDULE_USER_ID` + `SCHEDULE_SOURCE`）。
- 记忆/对话历史默认按 `user_id` 命名空间隔离（默认 `root`）。
- CLI 不再本地执行过期切分；过期由 automation 常驻进程统一处理。

## 配置要点

主配置文件：`config.yaml`（参考 `config.example.yaml`）。

常用字段：

| 字段 | 说明 |
|---|---|
| `llm.provider` | `doubao` 或 `qwen` |
| `llm.api_key` | LLM 密钥 |
| `llm.model` | 模型名或端点 ID |
| `time.timezone` | 时区（默认 `Asia/Shanghai`） |
| `storage.data_dir` | 本地数据目录 |
| `memory.*` | 会话总结与记忆策略 |
| `automation.jobs` | 自动化任务定义 |
| `mcp.enabled` / `mcp.servers` | MCP 客户端与远端工具 |

## 项目结构（简版）

```text
src/schedule_agent/
├── automation/      # 调度/队列/SessionManager/Gateway
├── cli/             # 交互式命令行
├── core/
│   ├── agent/       # Agent 主循环
│   ├── interfaces/  # Core 协议与命令模型
│   ├── adapters/    # ScheduleAgent -> CoreSession 适配
│   ├── tools/       # 工具系统
│   ├── llm/         # LLM 客户端
│   └── memory/      # 记忆与会话总结
├── models/          # Event/Task
└── storage/         # JSON 持久化
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
