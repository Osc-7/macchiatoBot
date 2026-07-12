# 远程工作区

远程工作区让 macchiatoBot daemon 操作另一台机器上由用户授权的目录。daemon 仍负责 LLM、记忆、会话、飞书、调度和权限流；本机只运行轻量 `macchiato-remote` worker，在授权目录内提供 bash/file 能力。

## 部署角色

| 角色 | 包 | 运行位置 | 负责 |
|---|---|---|---|
| 完整 bot | `macchiato-bot` | 云服务器、开发机或本地完整安装 | daemon、LLM、记忆、前端、调度、remote gateway |
| 仅 worker | `macchiato-remote` | 笔记本、工作站、集群节点 | 授权工作区访问、本机 shell/file runtime |

一台装了完整 bot 的机器也可以运行 `macchiato-remote start`，把某个目录暴露给另一个 daemon。

## 服务端

照常启动 daemon：

```bash
uv run automation_daemon.py
# 或安装后：
macchiato-daemon
```

启用远程模式时，daemon 会暴露 remote worker WebSocket 网关。默认配置：

| 设置 | 默认值 | 作用 |
|---|---|---|
| `MACCHIATO_REMOTE_HOST` | `0.0.0.0` | 监听地址 |
| `MACCHIATO_REMOTE_PORT` | `9380` | 监听端口 |
| `MACCHIATO_REMOTE_TOKENS` | 空 | 逗号分隔的 `login=token` 覆盖 |
| `MACCHIATO_REMOTE_TOKEN` | 空 | 共享兜底 token |
| `MACCHIATO_REMOTE_LOGIN_BOOTSTRAP_TOKEN` | 空 | bootstrap 登录交换 |
| `MACCHIATO_REMOTE_LOGIN_APPROVER_SECRET` | 空 | 手动审批面板 secret |

同一端口也提供：

- `GET /remote/healthz`
- `GET /remote/login`

## 安装 worker

推荐只安装 worker：

```bash
uv tool install macchiato-remote
macchiato-remote --version
```

从仓库开发：

```bash
uv sync
uv run macchiato-remote status
uv tool install -e packages/macchiato-remote
```

仅 worker 的机器不需要 `config/config.yaml`、`.env`、飞书配置、LLM key 或 automation daemon。

## 远程 MCP（协议 v3 / macchiato-remote>=0.2.7）

远程工作区激活后，daemon 可以把声明为 `location: remote` 的 MCP 挂到当前会话；**进程在 worker 机器上启动**，agent 仍像普通工具一样调用。

1. Worker 安装 MCP 能力：`uv tool install 'macchiato-remote[mcp]==0.2.7'`
2. 在授权工作区写 `{workspace}/.macchiato/mcp.yaml`（`open_workspace` 会生成空模板）：

```yaml
mcp:
  servers:
    - name: local_chrome          # 必须与 daemon 配置同名
      enabled: true
      command: npx
      args: ["-y", "chrome-devtools-mcp@latest"]
```

3. Daemon `config.yaml` 声明同名 server：

```yaml
mcp:
  servers:
    - name: local_chrome
      location: remote
      attach_on: remote_use
      enabled: true
      tool_name_prefix: chrome
```

4. `/remote-use <login> <path>` 后自动挂载；也可用 `/mcp list|attach|detach|reload` 手动管理。
5. `/remote-use` 登录仍只用现有 worker token；`env` 仅当该 MCP 自己需要第三方 API key 时才填写。

## 配置登录别名

选择一个稳定别名，例如 `personal`、`work-mbp`、`studio-linux`。这个别名就是 `/remote-use` 使用的值。

在 daemon 机器上生成 token：

```bash
uv run macchiato-remote gen-token --login personal
# 可选：uv run macchiato-remote gen-token --login personal --bytes 48
```

命令只会把 sha256 摘要写入 `data/automation/remote_worker_tokens.json`。明文 token 只打印一次；把它传给本机 worker：

```bash
macchiato-remote login   --server http://your-macchiato-server.example.com:9380   --login personal   --token '<粘贴 gen-token 输出>'
```

也可以使用位置参数形式：

```bash
macchiato-remote login your-macchiato-server.example.com:9380 --login personal
```

不传 `--token` 时，CLI 会启动 device-login 流程。

## 登录方式

Bootstrap 交换：

1. 服务端设置 `MACCHIATO_REMOTE_LOGIN_BOOTSTRAP_TOKEN`。
2. 执行 `macchiato-remote login <server> --login <alias> --auth-token '<bootstrap-token>'`。
3. 服务端校验 bootstrap token 并签发 worker token。
4. CLI 将 worker token 保存到本机配置。

手动审批面板：

1. CLI 向 `/remote/login/start` 申请一次性 code。
2. 打开 `/remote/login`，用 `MACCHIATO_REMOTE_LOGIN_APPROVER_SECRET` 审批。
3. CLI 轮询 `/remote/login/poll`，拿到 onboarding token 并保存。

飞书审批：

1. 配好服务端飞书 bot 与 `feishu.automation_activity_chat_id`。
2. 用 `MACCHIATO_REMOTE_LOGIN_APPROVER_OPEN_IDS` 或 `MACCHIATO_REMOTE_LOGIN_APPROVER_USER_IDS` 配审批人。
3. 执行 `macchiato-remote login <server-ip>:9380 --login <alias>`。
4. 服务端发送飞书审批卡片。
5. 审批通过后 CLI 自动保存 token。

## 运行 worker

前台调试：

```bash
macchiato-remote start
```

日常后台运行：

```bash
macchiato-remote start --background
macchiato-remote status
macchiato-remote stop
```

后台日志位于 `~/.local/state/macchiato/remote-worker.log`，pid 文件位于 `~/.local/state/macchiato/remote-worker.pid`。

## 在 CLI 或飞书中使用

worker 已连接后，只切换当前 session 到远程模式：

```text
/remote-use personal ~/Project
/remote-use personal ~/Project --profile dev --ttl 30m
```

配套命令：

```text
/remote-status
/remote-release
/cloud-use
```

remote mode 是按 session 生效的，不会全局替换所有 core。启用后，`bash`、`read_file`、`write_file`、`modify_file` 会被视为远程工作区操作；`/workspace`、`~` 和相对路径指向本机授权目录。

## 权限档位

| 档位 | 用途 |
|---|---|
| `strict` | 只暴露显式授权工作区，尽量减少宿主机暴露 |
| `dev` | 默认开发档位，允许项目目录和常见工具链/cache 挂载 |
| `host-user` | 短时、本机确认后，以本机用户权限操作 |
| `host-admin` | 最高风险档位，仅用于逐条确认的管理员动作 |

默认档位是 `dev`。更高权限应短时、显式、可审计，不作为默认工作区使用。

## SSH 隧道模式

公网 WebSocket 不稳定时，可以保存一次 SSH 隧道配置：

```bash
macchiato-remote login   --server http://203.0.113.10:9380   --login macbook   --token '<与 daemon 相同的 token>'   --ssh-tunnel ubuntu@203.0.113.10

macchiato-remote start --background
```

配置 `--ssh-tunnel` 后，`start`、`start --background`、`probe` 会自动打开 `127.0.0.1:19380 -> SSH_HOST:127.0.0.1:9380`。可用 `--ssh-local-port`、`--ssh-remote-host`、`--ssh-remote-port` 调整；用 `--clear-ssh-tunnel` 移除。

## 排障

先跑握手探测：

```bash
macchiato-remote probe
```

如果首行是 `HTTP/1.1 101 Switching Protocols`，说明 TCP 与 WebSocket 升级正常，继续查 `websockets` 栈或运行环境。

如果 `probe` 输出乱码/HTML 或卡住，检查 Clash TUN、全局 VPN 或代理链是否劫持原始 IP 流量。TUN 捕获全流量时，仅清空 shell 里的 `http_proxy` 不够。

如果关掉 Clash 后 `probe` 超时，怀疑 VPN/TUN 退出后残留路由或 `utun` 接口。重启、换网络，再确认云上安全组允许入站 TCP `9380`。

如果只有开 Clash 时网络通，可能是 TUN 退出没有恢复默认路由。用客户端正常退出路径，或重启恢复路由表。

日常需要常开 TUN 时，在最终 `MATCH` 规则前加高优先级直连规则：

```yaml
rules:
  - IP-CIDR,203.0.113.10/32,DIRECT,no-resolve
```

把 IP 换成 daemon 公网 IP，重载 Clash 后再运行 `macchiato-remote probe`。
