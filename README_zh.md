# macchiatoBot

中文 | [English](README.md)


一个基于 LLM 的人工智能助手，采用 **Tool-driven + Kernel 调度** 架构，强调可控、可扩展与长期运行稳定性。

macchiatoBot 将 AgentCore 的“推理”与 Kernel 的“执行/权限/回收”分离：

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
# 1) 安装依赖
uv sync --all-groups
# 可选：使用快捷脚本
# source init.sh

# 2) 复制配置
cp config/config.example.yaml config/config.yaml
cp .env.example .env

# 3) 配置密钥（在 .env 中）
# 例如：OPENAI_API_KEY / DASHSCOPE_API_KEY / KIMI_CODE_API_KEY / GEMINI_API_KEY / DEEPSEEK_API_KEY

# 4) 启动 automation daemon（CLI/飞书都依赖它）
uv run automation_daemon.py

# 5) 启动前端
uv run main.py                 # CLI
uv run feishu_ws_gateway.py    # 飞书长连接网关（可选）

# 6) 单条命令模式（仍通过 daemon）
uv run main.py 明天下午3点开会

# 可选：覆盖默认用户/来源（默认 user_id=root, source=cli）
SCHEDULE_USER_ID=root SCHEDULE_SOURCE=cli uv run main.py
```

其中 `init.sh` 只是便捷脚本：会执行 `uv sync`、导出 `PYTHONPATH`，并把 `.env` 加载到当前 shell。只要你的环境已经准备好，就不需要每次运行命令前都 `source init.sh`。

## 运行模式

### 1) 后台进程

```bash
uv run automation_daemon.py
```

### 2) 交互式 CLI（需先启动 daemon）

```bash
uv run main.py
```

若 daemon 未启动，CLI 会报错并退出。

Daemon 会执行：

- 从 `config/config.yaml` 同步自动化 job 定义（沿用现有调度链路）
- 调度器按规则入队，消费者执行队列任务
- 暴露本地 IPC（Unix Socket）给 CLI / 其他前端
- 在 automation 进程内统一执行 session expired 检查与切分（idle + 4am）

## CLI / 飞书斜杠命令

CLI 与飞书都支持一组常用斜杠命令（通过 IPC）：

- `/help`：查看帮助
- `/model` / `/model list`：列出模型（主模型标记 `*`，vision provider 标记 `V`）
- `/model <model name>`：切换主对话模型（通常使用 label）
- `/session`：显示当前会话
- `/session whoami`：显示当前 user/source/session
- `/session list`：列出已加载会话
- `/session new [id]`：创建并切换新会话
- `/session switch <id>`：切换到已有会话
- `/session delete <id>`：删除会话记录（不删除历史日志文件）

示例：

```bash
/session
/session new cli:work
/session list
/session switch cli:root
```

说明：

- 推荐通过 `uv run automation_daemon.py` 运行，跨终端共享会话视图。
- 会话列表通过共享注册表跨终端可见（同一 `SCHEDULE_USER_ID` + `SCHEDULE_SOURCE`）。
- 记忆/对话历史默认按 `user_id` 命名空间隔离（默认 `root`）。
- CLI 不再本地执行过期切分；过期由 automation 常驻进程统一处理。

排障（CLI 连不上 daemon）：

```bash
# 1) 检查 systemd daemon 状态
sudo systemctl status macchiato-automation.service --no-pager

# 2) 检查是否覆盖了 socket（通常应为空）
echo "$SCHEDULE_AUTOMATION_SOCKET"

# 3) 若未使用 systemd，可前台启动 daemon
uv run automation_daemon.py
```

若设置了 `SCHEDULE_AUTOMATION_SOCKET`，请确保它与 daemon 实际监听的 socket 路径一致。

## 配置要点

主配置文件：`config/config.yaml`（参考 `config/config.example.yaml`）。

常用字段：


| 字段                            | 说明                     |
| ----------------------------- | ---------------------- |
| `llm.active`                  | 当前主对话 provider 配置名（key） |
| `llm.vision_provider`         | 识图 provider（为空自动选择 vision=true） |
| `llm.providers_dir`           | provider 目录（默认 `llm/providers.d`） |
| `llm.providers`               | 内联 provider（可与目录合并，后加载覆盖前加载） |
| `time.timezone`               | 时区（默认 `Asia/Shanghai`） |
| `storage.data_dir`            | 本地数据目录                 |
| `memory.*`                    | 会话总结与记忆策略              |
| `automation.jobs`             | 自动化任务定义                |
| `mcp.enabled` / `mcp.servers` | MCP 客户端与远端工具           |
| `feishu.*`                    | 飞书网关与交互卡片配置            |
| `canvas.*`                    | Canvas 集成配置            |
| `shuiyuan.*`                  | 水源社区集成配置               |
| `file_tools.*` / `command_tools.*` | 文件/命令工具权限与工作区隔离 |

LLM 推荐配置方式：

- 在 `config/llm/providers.d/*.yaml` 维护可选模型（qwen/kimi/deepseek/gemini/openai/sjtu 等）
- 在 `config/config.yaml` 用 `llm.active` 选择默认主模型
- 运行时用 `/model <name>` 动态切换
- API key 统一写入仓库根 `.env`（参考 `.env.example`）

多模态相关：

- `multimodal.*` 控制多模态行为（如图片大小限制、超时）
- `recognize_image` 默认使用 `llm.vision_provider`
- `attach_media` 可在下一轮对话附带图片/文件


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
uv sync --all-groups
uv run pytest tests/ -v
```

如果你希望当前 shell 自动继承 `.env` 里的变量，可以在进入这个 shell 后执行一次 `source init.sh`，或者用你自己的方式加载 `.env`。

## 部署（systemd）

部署文档已拆分，请直接查看：

- [deploy/README.md](deploy/README.md)

## 飞书接入

飞书接入说明已拆分，请查看：

- [docs/feishu_zh.md](docs/feishu_zh.md)

## MCP 本地入口

```bash
uv run mcp_server.py
```

如果要让 Agent 调用本地 MCP 工具，可在 `config/config.yaml` 配置 `mcp.servers`（stdio）。

---

许可证：MIT

开发规范见 [AGENTS.md](AGENTS.md)。
