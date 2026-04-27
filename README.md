# macchiatoBot

English | [中文](README_zh.md)

macchiatoBot is an LLM assistant built around a tool-driven, kernel-style runtime. The project is designed for long-running use: interactive chat, scheduled automation, multi-session concurrency, and frontend integrations all go through the same execution pipeline.

The current codebase separates reasoning from execution:

- `AgentCore` handles prompt building, LLM calls, memory recall, and the tool-calling loop.
- `AgentKernel` executes tools, enforces tool visibility and permissions, and handles context compression.
- `KernelScheduler` and the automation layer own session lifecycle, IPC, queueing, and background jobs.

## What It Supports

- Interactive CLI backed by a long-running daemon
- Feishu WebSocket gateway with shared sessions and slash commands
- Local MCP stdio server
- Scheduled jobs and notification-oriented automation
- Tool registry with file, bash, memory, web, multimodal, Canvas, Shuiyuan, and SJTU helpers
- Multi-provider LLM routing with runtime model switching
- Working memory, chat history retrieval, and long-term/content memory
- Subagent and multi-agent tooling

## Architecture

```text
Frontend / External Trigger
  ├─ CLI
  ├─ Feishu gateway
  ├─ MCP stdio server
  └─ Automation jobs
          │
          ▼
Automation IPC / Core Gateway / Task Queue
          │
          ▼
KernelScheduler / CorePool / SessionSummarizer
          │
          ▼
AgentKernel
  ├─ tool execution
  ├─ permission checks
  ├─ path resolution
  └─ context compression
          │
          ▼
AgentCore
  ├─ prompt assembly
  ├─ LLM provider routing
  ├─ working set + tool selection
  ├─ memory recall / persistence
  └─ multi-turn reasoning loop
```

### Layer Map

| Layer | Main modules | Responsibility |
|---|---|---|
| Frontend | `main.py`, `feishu_ws_gateway.py`, `mcp_server.py`, `src/frontend/*` | User entrypoints and channel-specific adapters |
| Automation | `automation_daemon.py`, `src/system/automation/*` | IPC server/client, scheduled jobs, queue consumption, session rotation |
| Kernel | `src/system/kernel/*` | Core pooling, scheduling, terminal shell, output routing, summarization |
| Agent runtime | `src/agent_core/agent/*`, `src/agent_core/llm/*`, `src/agent_core/context/*` | Prompting, LLM loop, checkpoints, multimodal staging, session state |
| Tooling | `src/system/tools/*`, `src/agent_core/tools/*`, `src/agent_core/mcp/*` | Tool registry, permissions, MCP proxying, runtime tools |
| Integrations | `src/frontend/feishu/*`, `src/frontend/shuiyuan_integration/*`, `src/frontend/canvas_integration/*` | External platform integration and connector logic |

## Repository Layout

```text
src/
├── agent_core/
│   ├── agent/            # AgentCore, checkpoints, prompt builder, workspace/memory paths
│   ├── llm/              # Provider resolution and OpenAI-compatible adapters
│   ├── memory/           # Working memory, long-term memory, chat history DB
│   ├── tools/            # Core tools such as bash / ask_user / permission flow
│   ├── mcp/              # MCP client, pool, and proxy tools
│   └── prompts/          # System prompts and skills
├── system/
│   ├── automation/       # Daemon runtime, queue, IPC, connectors, config sync
│   ├── kernel/           # AgentKernel, scheduler, core pool, terminal
│   ├── tools/            # App-level tools: memory, web, canvas, shuiyuan, planner
│   └── multi_agent/      # Multi-agent registry and constants
└── frontend/
    ├── cli/              # Interactive CLI loop
    ├── feishu/           # Feishu gateway, cards, callbacks, routing
    ├── mcp_server/       # Local MCP stdio server
    ├── canvas_integration/
    └── shuiyuan_integration/
```

## Quick Start

### 1. Install dependencies

```bash
uv sync --all-groups
```

Optional helper:

```bash
source init.sh
```

`init.sh` is a convenience script. It runs `uv sync`, exports `PYTHONPATH`, and loads `.env` into the current shell. You do not need to source it before every command if your environment is already set up.

### 2. Prepare config

```bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

Then fill provider keys in `.env`, for example `OPENAI_API_KEY`, `DASHSCOPE_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, or `KIMI_CODE_API_KEY`.

### 3. Start the daemon

```bash
uv run automation_daemon.py
```

The daemon is the shared runtime for:

- CLI requests
- Feishu requests
- scheduled automation jobs
- session expiration and rotation

### 4. Start a frontend

```bash
uv run main.py
uv run feishu_ws_gateway.py
```

For a single command:

```bash
uv run main.py "schedule a meeting tomorrow at 3pm"
```

Optional session identity override:

```bash
SCHEDULE_USER_ID=root SCHEDULE_SOURCE=cli uv run main.py
```

## Runtime Model

### Daemon-first workflow

`main.py` is a thin IPC client. It does not run the full agent locally; it connects to `automation_daemon.py` over a Unix socket. If the daemon is not running, CLI exits with an error.

### What the daemon does

- loads config and tool registry
- syncs `automation.jobs` from `config/config.yaml`
- runs queue consumers and job scheduling
- hosts the IPC server used by CLI and other frontends
- centralizes session expiration, rotation, and summarization

## Common Commands

CLI and Feishu share the same slash command surface through IPC:

- `/help`
- `/model`
- `/model list`
- `/model <name>`
- `/session`
- `/session whoami`
- `/session list`
- `/session new [id]`
- `/session switch <id>`
- `/session delete <id>`

Example:

```bash
/session
/session new cli:work
/session list
/session switch cli:root
```

## Configuration

Main config: `config/config.yaml`. Example: `config/config.example.yaml`.

Important areas:

| Key | Purpose |
|---|---|
| `llm.*` | active provider, vision provider, provider fragments, request defaults |
| `multimodal.*` | multimodal input limits and timeout |
| `agent.*` | iteration limits, subagent caps, working set size |
| `tools.*` | core tool exposure and template-based tool sets |
| `memory.*` | working-memory limits, recall policy, persistent memory |
| `automation.jobs` | scheduled jobs managed by the daemon |
| `file_tools.*` | file read/write/modify controls |
| `command_tools.*` | bash enablement, workspace isolation, writable roots |
| `canvas.*` | Canvas integration |
| `shuiyuan.*` | Shuiyuan integration |
| `sjtu_jw.*` | SJTU course schedule sync |
| `mcp.*` | external MCP server configuration |
| `feishu.*` | Feishu app and gateway settings |

Provider fragments live in `config/llm/providers.d/*.yaml`. The active provider is selected by `llm.active`, and can be changed at runtime with `/model <name>`.

## MCP

Local MCP entry:

```bash
uv run mcp_server.py
```

External MCP servers can be configured under `mcp.servers` in `config/config.yaml`.

## Development

```bash
uv sync --all-groups
uv run pytest tests/ -v
```

The repository currently has broad coverage across agent runtime, automation, permissions, multimodal handling, frontend integrations, and tool behavior.

If you want the shell to inherit values from `.env`, either use `source init.sh` once for that shell, or load `.env` with your own workflow.

## Additional Docs

- [Feishu integration](docs/feishu.md)
- [Deployment / systemd](deploy/README.md)
- [Development guidelines](AGENTS.md)

## License

MIT
