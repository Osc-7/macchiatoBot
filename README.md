# macchiatoBot

English | [中文](README_zh.md)


An LLM-based assistant built with a **Tool-driven + Kernel scheduling** architecture, focused on control, extensibility, and long-running stability.

macchiatoBot separates “reasoning” from “execution/permissions/reclamation”:

- AgentCore handles dialogue and decision-making
- Kernel handles tool execution, context compression, and lifecycle management
- Scheduler handles multi-session concurrency and TTL-based eviction

It adopts a **kernel-style architecture** that unifies scheduling, permissions, and reclamation within the Kernel/Scheduler layer.

- **Kernel-style architecture**: reasoning and IO are decoupled; tools are executed by the Kernel
- **Multi-session concurrency + TTL eviction**: suitable for daemon mode and multi-terminal usage
- **Integrated automation pipeline**: cron jobs, queues, and IPC in one chain
- **Extensible tool system**: unified registry, permission filtering, MCP integration
- **Memory & context strategies**: working-memory compression, chat history search, long-term memory
- **Multi-endpoint access**

## Quick Start

```bash
# 1) Initialize
source init.sh

# 2) Configure
cp config/config.example.yaml config/config.yaml
# Fill llm.api_key / llm.model (or override via env vars)

# 3) Start automation daemon (recommended)
python automation_daemon.py

# 4) Start frontends
python main.py # CLI
python feishu_ws_gateway.py # Feishu gateway

# 5) One-shot command
python main.py "schedule a meeting tomorrow at 3pm"

# Optional: override default user/source (defaults: user_id=root, source=cli)
SCHEDULE_USER_ID=root SCHEDULE_SOURCE=cli python main.py
```

## Run Modes

### 1) Daemon

```bash
python automation_daemon.py
```

### 2) Interactive CLI

```bash
python main.py
```

The daemon will:

- Sync automation job definitions from `config/config.yaml`
- Enqueue jobs and execute them via the task queue
- Expose local IPC (Unix Socket) for CLI / other frontends
- Perform session expiration checks and rotation (idle + 4am)

## CLI Session Commands

CLI session commands (via IPC or local mode):

- `/session`: show current session
- `/session list`: list sessions
- `/session new [id]`: create and switch to a new session
- `/session switch <id>`: switch to an existing session

Example:

```bash
/session
/session new cli:work
/session list
/session switch cli:root
```

Notes:

- Recommended to run via `automation_daemon.py` for shared sessions across terminals.
- Session list is shared by registry (same `SCHEDULE_USER_ID` + `SCHEDULE_SOURCE`).
- Memory/chat history are isolated by `user_id` (default `root`).
- CLI no longer performs local expiration; the daemon owns session rotation.

## Configuration

Main config: `config/config.yaml` (see `config/config.example.yaml`).

Common fields:

| Field | Description |
|---|---|
| `llm.provider` | `doubao` or `qwen` |
| `llm.api_key` | LLM API key |
| `llm.model` | Model name or endpoint ID |
| `time.timezone` | Timezone (default `Asia/Shanghai`) |
| `storage.data_dir` | Local data directory |
| `memory.*` | Summarization and memory settings |
| `automation.jobs` | Automation job definitions |
| `mcp.enabled` / `mcp.servers` | MCP client and servers |

## Architecture (High-Level)

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
AgentCore (LLM reasoning + decisions)
```

## Project Structure

```text
src/
├── agent_core/    # AgentCore, Kernel interface, tools, memory
├── system/        # KernelScheduler, CorePool, automation/runtime
└── frontend/      # CLI, Feishu, MCP
```

## Development & Tests

```bash
source init.sh
pytest tests/ -v
```

## MCP Local Entry

```bash
python mcp_server.py
```

To enable local MCP tools, configure `mcp.servers` in `config/config.yaml` (stdio).

---

License: MIT

Development guidelines: [AGENTS.md](AGENTS.md).
