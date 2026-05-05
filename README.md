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
- `/remote-use <login> [path]`
- `/remote-status`
- `/remote-release` or `/cloud-use`

Example:

```bash
/session
/session new cli:work
/session list
/session switch cli:root
```

## Remote Workspaces

Remote workspaces are the planned path for letting a cloud-hosted macchiatoBot
session operate on a user-authorized folder on another machine, such as your
laptop. The local machine runs a lightweight `macchiato-remote` worker; the
cloud daemon keeps the LLM, memory, Feishu, scheduler, and permission flow.

Current branch status:

- `macchiato-remote` is packaged as an independent CLI entrypoint (WebSocket client;
  install optional extra: `uv tool install ".[remote]"` or `uv sync --extra remote`).
- The automation daemon exposes a WebSocket gateway (default `0.0.0.0:9380`, overridable
  via `MACCHIATO_REMOTE_HOST` / `MACCHIATO_REMOTE_PORT`; set `MACCHIATO_REMOTE_TOKEN`
  for a shared secret sent as `?token=`).
- `/remote-use`, `/remote-status`, and `/remote-release` open or release a remote
  workspace over IPC / Feishu; when active, `bash`, `read_file`, `write_file`, and
  `modify_file` are routed to the connected worker.
- When remote mode is active, the system prompt gets a remote workspace note at
  the very end, so the agent knows the current workspace backend changed.

### Install the Local Worker

During development, install the lightweight worker from this repository:

```bash
cd /path/to/macchiatoBot
uv tool install ".[remote]"
```

You can also run it directly from a checkout:

```bash
uv run macchiato-remote status
```

If you do not want to keep a full checkout on the local machine, build a wheel
on any machine that has the repository, copy the wheel to the target machine,
and install that wheel:

```bash
uv build --wheel
uv tool install dist/macchiato_bot-*.whl
```

The local worker does not need `config/config.yaml`, `.env`, Feishu settings,
LLM keys, or the automation daemon. Those stay on the cloud server.

### Configure the Local Login

Choose a login alias. This is the value used by `/remote-use`; it is intentionally
not hard-coded to a device name.

Generate a shared secret for the daemon (`MACCHIATO_REMOTE_TOKEN`) and this CLI
(`--token`); you do not need to invent one by hand:

```bash
macchiato-remote gen-token
# optional: macchiato-remote gen-token --bytes 48
```

Use the first line of output on the server as `MACCHIATO_REMOTE_TOKEN`, and pass
the same value to `login --token`. Point `--server` at your daemon host including
the WebSocket port (default **9380**, e.g. `http://203.0.113.10:9380` or behind TLS).

```bash
macchiato-remote login \
  --server http://your-macchiato-server.example.com:9380 \
  --login personal \
  --token '<paste gen-token output>'
```

Check local configuration:

```bash
macchiato-remote status
```

Start the worker in the foreground while debugging:

```bash
macchiato-remote start
```

For daily use, run it as a lightweight background process (pid + log file, no
launchd/systemd required):

```bash
macchiato-remote start --background
macchiato-remote status
macchiato-remote stop
```

Background logs are written to `~/.local/state/macchiato/remote-worker.log`; the
pid file is `~/.local/state/macchiato/remote-worker.pid`.

If public WebSocket access is unreliable in your network, save an SSH tunnel
configuration once and let the worker manage the tunnel automatically (no daily
manual `ssh -L`):

```bash
macchiato-remote login \
  --server http://110.40.171.96:9380 \
  --login macbook \
  --token '<same token as daemon>' \
  --ssh-tunnel ubuntu@110.40.171.96

macchiato-remote start --background
```

With `--ssh-tunnel` configured, `start`, `start --background`, and `probe` open
`127.0.0.1:19380 -> SSH_HOST:127.0.0.1:9380` automatically, and the worker
connects through that local tunnel. Tune it with `--ssh-local-port`,
`--ssh-remote-host`, and `--ssh-remote-port`; remove it with `--clear-ssh-tunnel`.

If you see `InvalidMessage('did not receive a valid HTTP response')`, run:

```bash
macchiato-remote probe
```

This sends the same WebSocket upgrade over a **stdlib blocking socket** (no
`asyncio`, no `HTTP_PROXY`). If the first line is `HTTP/1.1 101 Switching Protocols`,
the path to the server is fine and the failure is specific to the `websockets`
stack or your environment. If `probe` shows garbage/HTML or hangs, check **Clash
TUN / VPN** (turning off **TUN mode** or bypassing the server IP often fixes it;
empty `http_proxy` in the shell is **not** enough when TUN captures all IP traffic).
The `start` command also prints the **underlying `__cause__`** of handshake errors
after upgrading the package.

If Clash is off but you only see **`TimeoutError`** (and `probe` also times out) while
`nc -zv` used to work, suspect **stale routes or utun leftovers after VPN/TUN** — reboot
the Mac, try another network (e.g. phone hotspot), and re-check `nc -zv` plus the cloud
security group for TCP **9380**.

If **`nc` works only while Clash is on and fails after quitting**, the TUN exit likely
**did not restore the default gateway**. Reboot the Mac or use Clash’s normal quit path
that restores routes; killing the app alone often leaves a broken routing table.

#### Keeping TUN on (recommended for daily use)

Add a **high-priority** rule so traffic to your daemon’s **public IP** uses `DIRECT`
(still inside TUN, but not steered into a broken proxy chain). Place it **before** the
catch-all `MATCH` rule, and replace the IP with yours:

```yaml
rules:
  - IP-CIDR,110.40.171.96/32,DIRECT,no-resolve
```

`no-resolve` makes the rule match by IP without DNS quirks. Reload Clash, then
`macchiato-remote probe` should show `HTTP/1.1 101 Switching Protocols`. If handshakes
still flake, try switching the **TUN stack** (`gvisor` vs `system`) in Clash Meta / Verge.

### Use From Feishu Or CLI

With the daemon reachable from your machine at the `--server` URL and the worker
running, switch the current session into remote workspace mode:

```text
/remote-use personal ~/Project
/remote-use personal ~/Project --profile dev --ttl 30m
```

Useful companion commands:

```text
/remote-status
/remote-release
/cloud-use
```

Remote mode is session-scoped. It does not globally change every core. While it
is active, the agent is instructed that `bash`, `read_file`, `write_file`, and
`modify_file` should be treated as operating on the remote workspace, with
`/workspace`, `~`, and relative paths referring to the authorized local folder.

Permission profiles are designed as:

| Profile | Intent |
|---|---|
| `strict` | Only the explicitly authorized workspace, minimal host exposure |
| `dev` | Developer-friendly sandbox with project access and common toolchain/cache mounts |
| `host-user` | Short-lived, user-confirmed access as the local OS user |
| `host-admin` | Highest-risk mode for explicit, per-command admin actions |

The default profile is `dev`. Higher-privilege modes should be short-lived and
audited; they are meant for explicit elevation, not as the default workspace.

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
