# macchiatoBot

English | [中文](README_zh.md)

macchiatoBot is a daemon-first, tool-driven LLM assistant. The daemon owns
sessions, scheduling, IPC, tool execution, permissions, memory, and frontend
integration; CLI, Feishu, MCP, and automation jobs all enter through that shared
runtime.

The repository currently publishes two installable surfaces:

| Package | Role | Commands |
|---|---|---|
| `macchiato-bot` | Full assistant runtime for cloud/dev/local bot use | `macchiato`, `macchiato-daemon`, `macchiato-remote` |
| `macchiato-remote` | Lightweight worker for exposing one authorized local workspace to a bot daemon | `macchiato-remote` |

In a checkout, the root `main.py` and `automation_daemon.py` files are thin
compatibility shims around the packaged entrypoints.

## Runtime Shape

```text
CLI / Feishu / MCP / automation trigger
        |
        v
Automation IPC + Core Gateway + task queue
        |
        v
KernelScheduler + CorePool
        |
        v
AgentKernel
  - tool execution
  - permission checks
  - path and remote-workspace routing
  - context compression
        |
        v
AgentCore
  - prompt assembly
  - LLM provider routing
  - memory recall
  - tool-calling loop
```

Short version: `AgentCore` thinks, `AgentKernel` executes, and the automation
daemon keeps the long-running process, sessions, queue, and IPC stable.

### Architecture Principles

- **Daemon-first runtime**: long-running state belongs to `macchiato-daemon` /
  `automation_daemon.py`, not to individual CLI invocations.
- **Frontend adapters stay thin**: CLI, Feishu, MCP, and automation jobs parse
  channel-specific input and then hand work to daemon IPC or the task queue.
- **Reasoning is separate from execution**: `AgentCore` builds prompts and talks
  to the LLM; `AgentKernel` executes tools, checks permissions, routes paths,
  and compresses context.
- **Remote workspace is a routing mode**: remote mode changes where selected
  tools run; it is not a second agent stack.
- **Release commits stay small**: runtime architecture changes land as normal
  feature/fix commits before a release commit handles versioning and packaging.

### Layer Map

| Layer | Main modules | Owns | Should not own |
|---|---|---|---|
| Frontend | `src/frontend/*`, root shims | Channel parsing, display, callbacks | Agent state, direct tool execution |
| Automation | `src/system/automation/*` | IPC, queues, job definitions, session registry, scheduling | LLM prompt details |
| Kernel | `src/system/kernel/*` | Core pooling, kernel requests, terminal shell, summarization | Provider selection details |
| Agent runtime | `src/agent_core/agent/*`, `src/agent_core/llm/*`, `src/agent_core/context/*` | Agent loop, prompts, providers, memory/context state | Frontend transport details |
| Tools | `src/agent_core/tools/*`, `src/system/tools/*`, `src/agent_core/mcp/*` | Tool definitions, validation, execution, MCP proxying | Release packaging |
| Remote worker | `src/macchiato_remote/*`, `src/agent_core/remote/*` | Remote protocol, worker registry, workspace routing | Full bot daemon state |

### Tool Boundary

Tools are exposed to the LLM through the registry, but the kernel remains the
authority for visibility, permission checks, path grants, local-vs-remote
routing, and large-result handling. That keeps the LLM loop simple: it requests
tool calls; the kernel decides how to execute them safely.

### Runtime State

Generated state stays out of source control:

| Path | Purpose |
|---|---|
| `data/` | persistent app data, sessions, automation repositories |
| `logs/` | daemon and gateway logs |
| `.macchiato/` | local command/job runtime state |
| `dist/`, `build/`, `*.egg-info/` | package build outputs |
| `.venv/`, `.pytest_cache/`, `__pycache__/` | local development artifacts |

For the longer design notes and contribution placement rules, see
[docs/architecture.md](docs/architecture.md).

## Repository Map

```text
src/
├── agent_core/          # Agent loop, prompts, memory, LLM providers, core tools
├── system/
│   ├── automation/      # Daemon runtime, IPC, queue, scheduler, repositories
│   ├── kernel/          # AgentKernel, KernelScheduler, CorePool, terminal
│   └── tools/           # App-level tools and tool registry assembly
├── frontend/            # CLI, Feishu, MCP, Canvas, Shuiyuan adapters
├── macchiato_bot_cli/   # Packaged CLI and daemon entrypoints
└── macchiato_remote/    # Remote worker protocol, CLI, runtime

packages/macchiato-remote/
└── pyproject.toml       # Worker-only PyPI package built from src/macchiato_remote
```

## Quick Start From A Checkout

```bash
uv sync --all-groups
cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

Fill provider keys in `.env`, then start the daemon:

```bash
uv run automation_daemon.py
```

In another terminal, start a frontend:

```bash
uv run main.py
uv run main.py "schedule a meeting tomorrow at 3pm"
uv run feishu_ws_gateway.py
```

`source init.sh` is optional. It runs `uv sync`, exports `PYTHONPATH`, and loads
`.env` for the current shell.

## Installed Commands

After installing `macchiato-bot`, use:

```bash
macchiato-daemon
macchiato
macchiato "schedule a meeting tomorrow at 3pm"
macchiato-remote status
```

The CLI is an IPC client. If the daemon is not running, it exits instead of
starting a private agent process.

## Common Slash Commands

CLI and Feishu share the same slash-command surface through daemon IPC:

- `/help`
- `/model`, `/model list`, `/model <name>`
- `/session`, `/session whoami`, `/session list`
- `/session new [id]`, `/session switch <id>`, `/session delete <id>`
- `/remote-use <login> [path]`
- `/remote-status`
- `/remote-release` or `/cloud-use`

## Remote Workspaces

Remote workspace mode lets a cloud-hosted daemon operate on a user-authorized
folder on another machine. The full bot stays on the daemon host; the local
machine runs only `macchiato-remote`, which exposes bash/file capabilities for
that authorized workspace.

Read the setup, login modes, permission profiles, and troubleshooting notes in
[docs/remote-workspace.md](docs/remote-workspace.md).

## Configuration

Main config lives at `config/config.yaml`; start from
`config/config.example.yaml`. Provider fragments live under
`config/llm/providers.d/*.yaml`.

Important areas:

| Key | Purpose |
|---|---|
| `llm.*` | Active provider, vision provider, provider fragments, request defaults |
| `agent.*` | Iteration limits, subagent caps, working-set size |
| `tools.*` | Core tool exposure and template-based tool sets |
| `memory.*` | Working memory, recall policy, persistent memory |
| `automation.jobs` | Scheduled jobs managed by the daemon |
| `command_tools.*` | Bash enablement, workspace isolation, writable roots |
| `file_tools.*` | File read/write/modify controls |
| `mcp.*` | External MCP server configuration |
| `feishu.*` | Feishu app and gateway settings |

## Development

```bash
uv sync --all-groups
uv run pytest tests/ -v --tb=short
black --check src/ tests/
isort --check-only src/ tests/
```

Focused docs:

- [Architecture](docs/architecture.md)
- [Remote workspaces](docs/remote-workspace.md)
- [Feishu integration](docs/feishu.md)
- [Deployment / systemd](deploy/README.md)
- [Release process](deploy/RELEASING.md)
- [Development guidelines](AGENTS.md)

## License

MIT
