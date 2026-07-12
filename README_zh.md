# macchiatoBot

中文 | [English](README.md)

macchiatoBot 是一个 daemon-first、工具驱动的 LLM 助手。常驻 daemon 负责会话、调度、IPC、工具执行、权限、记忆和前端接入；CLI、飞书、MCP 与自动化任务都通过同一套运行时进入。

当前仓库有两个可安装入口：

| 包 | 作用 | 命令 |
|---|---|---|
| `macchiato-bot` | 完整 bot 运行时，适合云端、开发机或本机完整使用 | `macchiato`、`macchiato-daemon`、`macchiato-remote`、`macchiato-dashboard` |
| `macchiato-remote` | 轻量 worker，只把本机授权目录暴露给远端 daemon | `macchiato-remote` |

仓库根目录的 `main.py` 与 `automation_daemon.py` 只是兼容 `uv run` 的薄入口，实际实现位于 `src/macchiato_bot_cli/`。

## 运行架构

```text
CLI / 飞书 / MCP / 自动化触发
        |
        v
Automation IPC + Core Gateway + 任务队列
        |
        v
KernelScheduler + CorePool
        |
        v
AgentKernel
  - 工具执行
  - 权限检查
  - 路径与远程工作区路由
  - 上下文压缩
        |
        v
AgentCore
  - prompt 组装
  - LLM provider 路由
  - 记忆召回
  - tool-calling 循环
```

一句话：`AgentCore` 负责思考，`AgentKernel` 负责执行，automation daemon 负责常驻进程、会话、队列和 IPC。

### 架构原则

- **Daemon-first runtime**：长期状态属于 `macchiato-daemon` / `automation_daemon.py`，不属于某一次 CLI 调用。
- **前端适配层保持轻量**：CLI、飞书、MCP、自动化任务只处理渠道输入，再交给 daemon IPC 或任务队列。
- **推理与执行分离**：`AgentCore` 负责 prompt 与 LLM；`AgentKernel` 负责工具执行、权限检查、路径路由和上下文压缩。
- **远程工作区是工具路由模式**：remote mode 只改变部分工具在哪里运行，不引入第二套 agent 架构。
- **Release commit 保持小而清晰**：运行时架构变化先以 feature/fix 提交进入稳定基线，release commit 只处理版本和打包。

### 分层地图

| 层 | 主要模块 | 负责 | 不负责 |
|---|---|---|---|
| Frontend | `src/frontend/*`、根入口脚本 | 渠道解析、展示、回调 | Agent 状态、直接工具执行 |
| Automation | `src/system/automation/*` | IPC、队列、任务定义、会话注册、调度 | LLM prompt 细节 |
| Kernel | `src/system/kernel/*` | Core 池、kernel 请求、terminal、总结压缩 | provider 选择细节 |
| Agent runtime | `src/agent_core/agent/*`、`src/agent_core/llm/*`、`src/agent_core/context/*` | Agent 循环、prompt、provider、记忆/上下文状态 | 前端传输细节 |
| Tools | `src/agent_core/tools/*`、`src/system/tools/*`、`src/agent_core/mcp/*` | 工具定义、校验、执行、MCP 代理 | 发版打包 |
| Remote worker | `src/macchiato_remote/*`、`src/agent_core/remote/*` | 远程协议、worker 注册、工作区路由 | 完整 bot daemon 状态 |

### 工具边界

工具通过注册表暴露给 LLM，但 Kernel 始终负责可见性、权限检查、路径授权、本地/远程路由和大结果处理。这样 LLM 循环可以保持简单：它请求工具调用，Kernel 决定如何安全执行。

### 运行状态

生成状态不进入版本库：

| 路径 | 用途 |
|---|---|
| `data/` | 应用数据、会话、自动化仓库 |
| `logs/` | daemon / gateway 日志 |
| `.macchiato/` | 工作区本地状态：job 日志、日记、本机 rules/skills、scratch |
| `dist/`、`build/`、`*.egg-info/` | 打包构建产物 |
| `.venv/`、`.pytest_cache/`、`__pycache__/` | 本地开发产物 |

更完整的设计说明和新代码放置规则见 [docs/architecture_zh.md](docs/architecture_zh.md)。

## 项目结构

```text
src/
├── agent_core/          # Agent 循环、prompt、记忆、LLM provider、核心工具
├── system/
│   ├── automation/      # Daemon runtime、IPC、队列、调度、仓库
│   ├── kernel/          # AgentKernel、KernelScheduler、CorePool、terminal
│   └── tools/           # 应用级工具与工具注册表装配
├── frontend/            # CLI、飞书、MCP、Canvas、水源等前端适配
├── macchiato_bot_cli/   # 打包后的 CLI 与 daemon 入口
└── macchiato_remote/    # 远程 worker 协议、CLI 与 runtime

packages/macchiato-remote/
└── pyproject.toml       # 仅 worker 的 PyPI 包，从 src/macchiato_remote 构建
```

## 从仓库启动

```bash
uv sync --all-groups
cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

在 `.env` 中填好 provider key 后启动 daemon：

```bash
uv run automation_daemon.py
```

另开终端启动前端：

```bash
uv run main.py
uv run main.py "明天下午3点开会"
uv run feishu_ws_gateway.py
```

`source init.sh` 是可选便捷脚本：它会执行 `uv sync`、导出 `PYTHONPATH`，并把 `.env` 加载到当前 shell。

## 安装后的命令

安装 `macchiato-bot` 后可使用：

```bash
macchiato-daemon
macchiato
macchiato "明天下午3点开会"
macchiato-dashboard
macchiato-remote status
```

`macchiato-dashboard` 默认监听 `http://127.0.0.1:8765`，用于配置编辑和内核状态管理（可在页面中执行 spawn/cancel/kill）。

**公网部署**：与 `/remote/` 相同，合并进现有 Nginx `:80` 站点：

- `/login` — 登录页
- `/console/` — Web 控制台

详见 [deploy/nginx/README.md](deploy/nginx/README.md)。`dashboard_auth.yaml` 白名单 + HTTP 下 `secure_cookies: false`。

仪表盘能力（v1）：
- 配置文件在线编辑、改动统计、手动备份/恢复（自动保存前也会留档）
- 内核总览（active cores / queue / token usage / turn count）
- 会话运维（会话列表、点击填充、switch、clear context、spawn/cancel/kill）
- 模型运维（读取可用模型并一键切换）

CLI 是 daemon 的 IPC 客户端。daemon 不运行时，CLI 会退出，而不是偷偷启动一个私有 agent 进程。

## 常用斜杠命令

CLI 与飞书通过 daemon IPC 共用同一组斜杠命令：

- `/help`
- `/model`、`/model list`、`/model <name>`
- `/session`、`/session whoami`、`/session list`
- `/session new [id]`、`/session switch <id>`、`/session delete <id>`
- `/remote-use <login> [path]`
- `/remote-status`
- `/remote-release` 或 `/cloud-use`

## 远程工作区

远程工作区让云端 daemon 操作另一台机器上的用户授权目录。完整 bot 仍在云端；本机只运行 `macchiato-remote`，提供该授权目录下的 bash/file 能力。

安装、登录方式、权限档位和网络排障见 [docs/remote-workspace_zh.md](docs/remote-workspace_zh.md)。

## 配置

主配置文件是 `config/config.yaml`，从 `config/config.example.yaml` 复制开始。Provider 片段位于 `config/llm/providers.d/*.yaml`。

常用区域：

| 字段 | 作用 |
|---|---|
| `llm.*` | 当前 provider、vision provider、provider 片段、请求默认值 |
| `agent.*` | 最大迭代、subagent 限制、working set 大小 |
| `tools.*` | 核心工具暴露与模板化工具集 |
| `memory.*` | 工作记忆、召回策略、持久记忆 |
| `automation.jobs` | daemon 管理的定时任务 |
| `command_tools.*` | bash 开关、工作区隔离、可写根目录 |
| `file_tools.*` | 文件读写改权限 |
| `mcp.*` | 外部 MCP server 配置 |
| `feishu.*` | 飞书应用与网关配置 |

## 开发

```bash
uv sync --all-groups
uv run pytest tests/ -v --tb=short
black --check src/ tests/
isort --check-only src/ tests/
```

专项文档：

- [架构说明](docs/architecture_zh.md)
- [远程工作区](docs/remote-workspace_zh.md)
- [飞书接入](docs/feishu_zh.md)
- [部署 / systemd](deploy/README.md)
- [发版流程](deploy/RELEASING.md)
- [开发规范](AGENTS.md)

## License

MIT
