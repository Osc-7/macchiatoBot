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
- `/remote-use <login> [path]`：将当前会话切换到远程工作区模式
- `/remote-status`：查看当前会话远程工作区状态
- `/remote-release` / `/cloud-use`：释放远程工作区，恢复云端工作区

示例：

```bash
/session
/session new cli:work
/session list
/session switch cli:root
```

## 远程工作区（Remote Workspace）

远程工作区用于让部署在云服务器上的 macchiatoBot 会话操作另一台机器上由用户授权的目录，例如你的电脑。云端仍负责 LLM、记忆、飞书、调度和权限流；本机只运行一个轻量的 `macchiato-remote` worker，负责暴露本机工作区、bash/file 能力和本机确认。

当前分支状态：

- 已新增独立的 `macchiato-remote` CLI（WebSocket 客户端；需 `uv sync --extra remote` 或 `uv tool install ".[remote]"` 安装 `websockets`）。
- 云上 `automation_daemon` 暴露 WebSocket 网关（默认监听 `0.0.0.0:9380`，可用 `MACCHIATO_REMOTE_HOST` / `MACCHIATO_REMOTE_PORT` 覆盖；建议设置按机器区分的 `MACCHIATO_REMOTE_TOKENS`，也可用共享兜底 `MACCHIATO_REMOTE_TOKEN`）。
- 已接入 `/remote-use`、`/remote-status`、`/remote-release`；启用后 `bash`、`read_file`、`write_file`、`modify_file` 路由到已连接的 worker。
- 当某个 session 启用 remote mode 时，会在 system prompt 末尾追加远程工作区说明。

### 在本机安装 worker

开发阶段可以从本仓库安装轻量 worker：

```bash
cd /path/to/macchiatoBot
uv tool install ".[remote]"
```

也可以直接从 checkout 运行：

```bash
uv run macchiato-remote status
```

如果你不想在本机保留完整仓库，可以在任意有仓库的机器上构建 wheel，把 wheel 拷到本机再安装：

```bash
uv build --wheel
uv tool install dist/macchiato_bot-*.whl
```

本机 worker 不需要 `config/config.yaml`、`.env`、飞书配置、LLM key 或 automation daemon。这些仍然只需要放在云服务器上。

### 配置本机登录别名

`login` 是 `/remote-use` 使用的可变登录别名，不需要固定成设备名。你可以使用 `personal`、`work-mbp`、`studio-linux` 等名字。

在云服务器项目根为这台机器生成 token（无需自己编长随机串）：

```bash
uv run macchiato-remote gen-token --login personal
# 可选：uv run macchiato-remote gen-token --login personal --bytes 48
```

命令会把该 `login` 的 token 摘要注册到云服务器
`data/automation/remote_worker_tokens.json`，明文 token 只会作为输出第一行显示一次。
将第一行保存到本机 worker 的 `login --token`。多台机器重复执行不同 `login` 即可。

`MACCHIATO_REMOTE_TOKENS='personal=<token1>,work-mbp=<token2>'` 和
`MACCHIATO_REMOTE_TOKEN=<token>` 仍可作为环境变量覆盖/兜底，但 systemd deploy 下
一般不需要再为 token 配环境变量。本机 `login` 时带上匹配的 `--token`。

```bash
macchiato-remote login \
  --server https://your-macchiato-server.example.com:9380 \
  --login personal \
  --token '<与云上相同的串>'
```

查看本机配置：

```bash
macchiato-remote status
```

启动 worker。调试时前台运行：

```bash
macchiato-remote start
```

日常使用可后台运行（写 pid 与日志，不依赖 launchd/systemd）：

```bash
macchiato-remote start --background
macchiato-remote status
macchiato-remote stop
```

后台日志位于 `~/.local/state/macchiato/remote-worker.log`，pid 文件位于
`~/.local/state/macchiato/remote-worker.pid`。

如果公网 WebSocket 被网络环境拦截，建议保存 SSH 隧道配置，让 worker 自己拉起隧道
（之后无需手敲 `ssh -L`）：

```bash
macchiato-remote login \
  --server http://203.0.113.10:9380 \
  --login macbook \
  --token '<与云上相同的串>' \
  --ssh-tunnel ubuntu@203.0.113.10

macchiato-remote start --background
```

配置了 `--ssh-tunnel` 后，`start` / `start --background` / `probe` 会自动打开
`127.0.0.1:19380 -> SSH_HOST:127.0.0.1:9380`，worker 实际连接本地隧道。可用
`--ssh-local-port`、`--ssh-remote-host`、`--ssh-remote-port` 调整；用
`--clear-ssh-tunnel` 移除保存的隧道配置。

若出现 `InvalidMessage('did not receive a valid HTTP response')`，先运行：

```bash
macchiato-remote probe
```

它会用 **标准库阻塞 socket** 发同样的 WebSocket 升级（**不读** `HTTP_PROXY`）。若输出首行是
`HTTP/1.1 101 Switching Protocols`，说明到服务器的 TCP/握手正常，再查 `websockets` 与运行环境。
若 `probe` 也是乱码/HTML，多半是 **Clash TUN / 全局 VPN** 劫持了 IP 流量（仅 `echo` 空
`http_proxy` **不够**）；可暂时关 TUN 或对 **daemon 所在公网 IP** 写直连规则。`start` 在升级后也会在
失败时打印握手异常的 **`__cause__`** 便于对照。

若关掉 Clash 后变成 **`TimeoutError` / `probe failed: TimeoutError`**（而以前 `nc` 能通），
常见是 **VPN/TUN 退出后路由表或 utun 残留**，对本机仍表现为「连公网 IP 一直等」。可：**重启
Mac**、或检查 `netstat -rn` / 暂时换网络（手机热点）再试 `nc -zv IP 9380`。云上同时确认
`ss -tlnp | grep 9380` 与 **安全组入站 TCP 9380** 未改。

若出现 **「开着 Clash 时 `nc` 通，关掉 Clash 反而 `nc` 不通」**：多半是 TUN 退出时 **未恢复默认网关/路由**，
流量仍指向已消失的接口。请 **重启 Mac** 或用 Clash 自带的正常退出/恢复路由选项；不要只靠强杀进程。

#### 需要常开 TUN、又不想为 remote 关 Clash 时

不要依赖「关 TUN / 关软件」来测通——在 Clash 配置里让 **云服务器公网 IP 直连** 即可（TUN 仍可常开）。

在 `rules:` 里把下面规则放在 **靠前位置**（必须在最后一条大 `MATCH` 订阅规则**之前**），并把 IP 换成你的 daemon 地址：

```yaml
rules:
  - IP-CIDR,203.0.113.10/32,DIRECT,no-resolve
```

- `no-resolve`：按 IP 命中，避免 DNS 策略导致规则不生效。
- 多台机器可写多条 `IP-CIDR`，或写一段 `/24` 等（注意安全面）。
- 改完后 **重载配置**，再跑 `macchiato-remote probe`，首行应为 `HTTP/1.1 101 Switching Protocols`。

若仍偶发握手异常，可在 Clash Meta / Verge 里尝试切换 **TUN 栈**（如 `gvisor` ↔ `system`），不同 macOS 版本兼容性有差异。

### 在飞书或 CLI 中使用

本机 worker 已连云、且 daemon 在跑时，在当前会话中切换到远程工作区：

```text
/remote-use personal ~/Project
/remote-use personal ~/Project --profile dev --ttl 30m
```

常用配套命令：

```text
/remote-status
/remote-release
/cloud-use
```

remote mode 是按 session 生效的，不会全局替换所有 core。启用后，agent 会被告知当前 `bash`、`read_file`、`write_file`、`modify_file` 应当视为在远程工作区运行；`/workspace`、`~` 和相对路径都指向本机授权目录。

权限档位设计：

| 档位 | 用途 |
| ---- | ---- |
| `strict` | 只暴露显式授权工作区，尽量减少宿主机暴露 |
| `dev` | 默认开发档位，允许项目目录和常见工具链/cache 挂载 |
| `host-user` | 短时、本机确认后，以本机用户权限操作 |
| `host-admin` | 最高风险档位，仅用于逐条确认的管理员动作 |

默认档位是 `dev`。更高权限应当短时、显式、可审计，不作为默认工作区使用。

CLI / 飞书说明：

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
