# Feishu Integration Guide

This document explains how to connect `macchiatoBot` to Feishu via the websocket gateway.

## 1. Prerequisites

- Project environment is initialized:
  - `uv sync --all-groups`
- Config files are prepared:
  - `config/config.yaml`
  - `.env`
- `automation daemon` can start normally.

## 2. Required configuration

Enable Feishu in `config/config.yaml` (see `config/config.example.yaml`):

- `feishu.enabled: true`
- Optional tuning: `feishu.domain`, `feishu.reply_format`, `feishu.assistant_reply_stream`, etc.

Put secrets in `.env`:

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_ENCRYPT_KEY`
- Optional: `FEISHU_AUTOMATION_CHAT_ID`

## 3. Startup order

```bash
# 1) Start daemon
uv run automation_daemon.py

# 2) Start Feishu gateway
uv run feishu_ws_gateway.py
```

Run daemon and gateway in separate terminals for clearer logs.

## 4. Common issues

### 4.1 Messages arrive in Feishu but no bot reply

- Check daemon status:
  - `sudo systemctl status macchiato-automation.service --no-pager`
  - or verify foreground daemon logs
- Confirm `feishu.enabled` is `true`
- Verify Feishu credentials in `.env`

### 4.2 Feishu slash commands are not responding

Feishu slash commands depend on daemon IPC. If daemon is unreachable, slash commands will fail too.

Quick local IPC check:

```bash
uv run main.py "ping"
```

If you get `pong`, IPC connectivity is fine.

## 5. systemd deployment

For long-running deployment via systemd (recommended), see:

- `deploy/README.md`

