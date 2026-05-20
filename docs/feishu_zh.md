# 飞书接入说明

本文档说明如何将 `macchiatoBot` 接入飞书长连接网关（WebSocket）。

## 1. 前置条件

- 已完成项目初始化：
  - `uv sync --all-groups`
- 已准备配置文件：
  - `config/config.yaml`
  - `.env`
- `automation daemon` 已可启动。

## 2. 必要配置

在 `config/config.yaml` 中启用飞书段（参考 `config/config.example.yaml`）：

- `feishu.enabled: true`
- 可选：`feishu.domain`、`feishu.reply_format`、`feishu.assistant_reply_stream` 等

敏感信息建议放在 `.env`：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_ENCRYPT_KEY`
- 可选：`FEISHU_AUTOMATION_CHAT_ID`

## 3. 启动顺序

```bash
# 1) 启动 daemon
uv run automation_daemon.py

# 2) 启动飞书网关
uv run feishu_ws_gateway.py
```

建议将 daemon 与飞书网关分别放在独立终端，便于观察日志。

## 4. 常见问题

### 4.1 飞书能收到消息，但机器人不回复

- 先检查 daemon 是否运行：
  - `sudo systemctl status macchiato-automation.service --no-pager`
  - 或前台进程日志是否正常
- 再检查 `feishu.enabled` 是否为 `true`
- 检查 `.env` 中飞书凭据是否完整

### 4.2 飞书 slash 命令无响应

飞书 slash 命令走 daemon IPC；若 daemon 不可达，slash 命令也会失败。

可先在本地验证 CLI 到 daemon 的连通性：

```bash
uv run main.py "ping"
```

若返回 `pong`，说明 IPC 通路正常。

## 5. systemd 部署

若使用 systemd 常驻部署（推荐），请直接参考：

- `deploy/README.md`

