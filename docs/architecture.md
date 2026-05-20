# Architecture

This document is the source of truth for the current macchiatoBot runtime layout.
The root README stays intentionally short; details live here.

## Principles

- The project is daemon-first. Long-running state belongs to `macchiato-daemon`
  / `automation_daemon.py`, not to individual CLI invocations.
- Frontends are adapters. CLI, Feishu, MCP, and automation jobs enter through
  daemon IPC or the automation queue.
- Reasoning and execution are separated. `AgentCore` builds prompts and talks to
  the LLM; `AgentKernel` executes tools and owns runtime controls.
- Remote workspace mode is a routing choice for selected tools, not a second
  agent architecture.

## Packages And Entrypoints

| Surface | Source | Purpose |
|---|---|---|
| `macchiato` | `src/macchiato_bot_cli/main.py` | CLI IPC client for interactive and single-command use |
| `macchiato-daemon` | `src/macchiato_bot_cli/daemon.py` | Long-running daemon for sessions, queue, IPC, scheduling, and frontend integration |
| `macchiato-remote` | `src/macchiato_remote/cli.py` | Worker CLI that exposes one authorized local workspace to a daemon |
| `uv run main.py` | `main.py` | Checkout shim for `macchiato` |
| `uv run automation_daemon.py` | `automation_daemon.py` | Checkout shim for `macchiato-daemon` |
| `uv run mcp_server.py` | `mcp_server.py` | Local MCP stdio server |
| `uv run feishu_ws_gateway.py` | `feishu_ws_gateway.py` | Feishu websocket gateway |

## Request Path

```text
Frontend / trigger
  - CLI
  - Feishu gateway
  - MCP stdio server
  - scheduled automation job
        |
        v
Automation layer
  - AutomationIPCServer / AutomationIPCClient
  - AutomationCoreGateway
  - AgentTaskQueue
  - AutomationScheduler
        |
        v
Kernel layer
  - KernelScheduler
  - CorePool
  - SessionSummarizer
        |
        v
AgentKernel
  - tool execution
  - permissions
  - path grants
  - context compression
  - output routing
        |
        v
AgentCore
  - prompt assembly
  - working set and tool selection
  - LLM provider routing
  - memory recall and persistence
  - multi-turn tool-calling loop
```

## Layer Map

| Layer | Main modules | Owns | Should not own |
|---|---|---|---|
| Frontend | `src/frontend/*`, root shims | Channel parsing, display, channel-specific callbacks | Agent state, direct tool execution |
| Automation | `src/system/automation/*` | IPC, queues, job definitions, session registry, scheduling | LLM-specific prompt logic |
| Kernel | `src/system/kernel/*` | Core pooling, kernel requests, terminal shell, summarization | Provider selection details |
| Agent runtime | `src/agent_core/agent/*`, `src/agent_core/llm/*`, `src/agent_core/context/*` | Agent loop, prompts, provider adapters, memory/context state | Frontend transport details |
| Tools | `src/agent_core/tools/*`, `src/system/tools/*`, `src/agent_core/mcp/*` | Tool definitions, validation, execution behavior, MCP proxying | Release packaging |
| Remote worker | `src/macchiato_remote/*`, `src/agent_core/remote/*` | Remote protocol, worker registry, workspace routing | Full bot daemon state |

## Tool Execution

Tools are registered through the tool registry and exposed to the LLM through the
kernel-controlled surface. The kernel is the authority for:

- whether a tool is visible in the current profile
- whether a path or command is allowed
- whether execution should be local or routed to a remote workspace
- how large tool results are summarized or overflowed

This keeps the LLM loop simple: it requests tool calls; the kernel decides how to
execute them safely.

## State And Generated Files

Runtime state is intentionally outside source control:

| Path | Purpose |
|---|---|
| `data/` | persistent app data, sessions, automation repositories |
| `logs/` | daemon and gateway logs |
| `.macchiato/` | local command/job runtime state |
| `dist/`, `build/`, `*.egg-info/` | package build outputs |
| `.venv/`, `.pytest_cache/`, `__pycache__/` | local development artifacts |

Docs under `docs/` are source files and should be tracked.

## Where To Put New Code

- New frontend behavior: `src/frontend/<channel>/`.
- New daemon/session/queue behavior: `src/system/automation/`.
- New scheduling/core-pool behavior: `src/system/kernel/`.
- New LLM loop, prompt, context, or memory behavior: `src/agent_core/`.
- New user-facing tools: prefer `src/system/tools/` for app-level tools and
  `src/agent_core/tools/` for low-level core tools.
- Remote worker features: `src/macchiato_remote/` for worker-side code,
  `src/agent_core/remote/` and kernel/tool routing for daemon-side integration.
- Packaging and release-only changes: `pyproject.toml`,
  `packages/macchiato-remote/pyproject.toml`, `.github/workflows/`, and `deploy/`.

## Release Boundary

Release commits should not redefine runtime architecture. Keep them focused on:

- version bumps
- package metadata
- CI/release workflow updates
- install and release documentation

When a release requires runtime changes, merge those runtime changes first as
normal feature/fix commits, then make a small release commit on the release base.
