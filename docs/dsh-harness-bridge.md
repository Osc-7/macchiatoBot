# Running macchiatoBot behind DSH Harness

This document records what it takes to use the macchiatoBot daemon as the agent backend of
[DeepSeek Harness](https://github.com/deepseek-ai) (DSH), and every pitfall we hit doing it.
It is written from a working deployment (DSH `0.1.5-rc.1`, macchiatoBot `0.2.3`, Ubuntu 24.04).

Nothing here is speculative: each section maps to a failure we observed, and the fix either
lives in this repository (see the accompanying PR) or in the DSH side of the bridge.

---

## 1. Why a bridge is needed at all

macchiatoBot is a daemon with its own IPC, its own scheduler, and its own frontends
(Feishu, forum). DSH is a different agent host: it owns the session log, the turn lifecycle,
the approval UI and the browser client.

Wiring them together means translating "one DSH step" into one `run_turn_stream` call, and
translating mbot's event stream back into DSH session events. The two sides disagree on five
things, and each disagreement is a visible bug if left alone:

| Difference | DSH assumes | macchiatoBot assumes | Symptom if unhandled |
|---|---|---|---|
| Step duration | a step is one model call + tools, events flow constantly | one stream may be silent for minutes while a tool runs, only 15 s heartbeats | long tool killed as "stalled" |
| Approval | there is an `approval` service with four outcomes | frontends receive `feishu_permission_notify` and render their own card | approval never resolves |
| Background jobs | no such concept | bash auto-backgrounds on timeout and returns a `job_id` | the job finishes and nobody is told |
| Session lifetime | not its concern | kernels are evicted by TTL | context lost after idle |
| Iteration cap | governed by the agent loop | `max_iterations` defaults to **10** | long task truncated mid-flight |

---

## 2. Topology

```
browser ──HTTPS──> nginx ──> dsh-lab.service (DSH --profile lab web)
                                   │  plugin: the bridge
                                   │  unix socket: run_turn_stream / tool_exec / resolve_permission
                                   ▼
                        macchiato-automation.service (mbot daemon)
                                   │  worker reverse connection over WS
                                   ▼
                        remote machines (gpu boxes, lab hosts)
```

Three systemd units: `dsh-lab`, `macchiato-automation`, and `lab-pty` (the file-channel bridge
used for FUSE mounts).

---

## 3. Configuration that must change

These are the defaults that silently truncate work. They are the hardest to diagnose because
nothing errors — the task just stops.

### 3.1 `agent.max_iterations` defaults to 10

Ten model calls per turn. Any multi-step task — a twelve-step refactor, a multi-file edit — is
cut off at step ten, and the frontend shows it as "the agent stopped on its own".

```yaml
agent:
  max_iterations: 300      # what the project author runs; the PR raises the default
```

### 3.2 Session TTL is read from the checkpoint, not from your config

```python
session_ttl = ckpt.remaining_ttl_seconds or float(
    getattr(self._config.agent, "session_expired_seconds", 1800))
```

`remaining_ttl_seconds` wins. While it stays at its old value, editing the config changes
nothing — the session keeps expiring on the old schedule. Migrate **both**
`remaining_ttl_seconds` and `core_profile.session_expired_seconds` in
`data/memory/<source>/<user>/checkpoint.json`.

Also note: with the daemon running, a checkpoint write can race your edit. Stop the service,
edit, start it again.

### 3.3 Other defaults that are too tight for real use

| Key | Default | Recommended | Why |
|---|---|---|---|
| `llm.request_timeout_seconds` | 120 | 300 | first token over a large context takes longer than 120 s |
| `memory.max_working_tokens` | 8000 | 500000 | working memory compresses constantly, dropping context |
| `agent.subagent_max_iterations` | 15 | 200 | sub-agents are cut off immediately |
| `agent.subagent_max_seconds` | 600 | 900 | same |
| `agent.working_set_size` | 6 | 30 | the working set is too small to be useful |

`llm.max_tokens` should be left alone: set it to what the provider actually supports.

---

## 4. Bugs that present as "the agent suddenly stopped"

### 4.1 Unbalanced quotes in a command abort the turn

```
bash_security._collect_workspace_write_grants → shlex.split(...)
→ ValueError: No closing quotation
→ Tool 'bash' raised an unhandled exception
```

`shlex.split` raises on `echo "unclosed`; the collector does not catch it, so it escapes the
tool. Treat an unparsable command as "grants nothing" and let the normal approval path run.

### 4.2 A heredoc command exits with rc=2 and empty output

The launcher wraps commands as `f"({command}) > {log} 2>&1"`. A command ending with a heredoc
terminator and no trailing newline becomes `EOF)`, which is a shell syntax error. Force a
trailing newline before wrapping.

### 4.3 A dropped remote worker aborts the turn

`_remote_exec_with_reopen` re-raises the first failure, and the reconnect-then-retry call is
not guarded either. One minute of network flapping is enough to kill a long task. Return a
retryable tool error instead (`REMOTE_WORKER_UNAVAILABLE`, naming the machine).

General rule: **a transient tool failure must never terminate the turn**; it should be an error
the agent can read and retry.

### 4.4 `chat_search` fails on hyphenated terms

FTS5 parses `gpu-worker` as `gpu - worker`. Quote every token before building the query.

### 4.5 A worker with a revoked token floods the log

Measured: 2508 WARNING lines in three hours. Log the first rejection per `(login, reason)` and
then at most once every ten minutes, reporting how many were suppressed.

---

## 5. The bridge itself

### 5.1 Budget timeouts per phase

mbot emits only heartbeats while a tool runs. Treating heartbeats as progress means a real
stall is never detected; treating "no content" as a stall kills long tools. Split the budget:

```js
idleMs: () => (pendingTools > 0 ? config.toolMs : config.stallMs)
stallMs = 600_000   // waiting for the model; we measured legitimate 307 s steps
toolMs  = 1800_000  // waiting for a tool; heartbeats only
```

Log the current budget in the heartbeat so an operator can see how long is left.

### 5.2 Approval must go through DSH's approval service

```js
const approval = ctx.approval ?? ctx.reflect.get("approval", false);
const outcome = await approval.request({ agent, toolName, reason, signal });
// → allowed-once | rejected | cancelled | unavailable
```

`ctx.approval` throws `cannot get property "approval" without inject`; use
`ctx.reflect.get(..., false)`. Map all four outcomes onto
`resolve_permission {permission_id, allowed, note}`, and send it on a separate connection so it
does not block the turn stream.

### 5.3 Background jobs must be able to wake the session

mbot auto-backgrounds a command on timeout and returns a `job_id`. The bridge should:

1. recognise it in the tool result (`{"job_id": ..., "status": "running"}`, possibly wrapped in
   `data`);
2. poll `tool_exec {tool:"bash", args:{job_status}}` every 10 s until a terminal state;
3. call `agent.followup(message)`.

`followup` goes through `send(msg, "next-turn", true) → wakeDriver()`, which **starts a new turn
when the agent is idle**; it is not queued until the user speaks.

`tool_exec` must carry a `client_id` or the IPC rejects it.

### 5.4 Tool events must be appended at their real timestamps

DSH's "tool call time" is `node.time - node.callTime` (result event minus call event). If the
bridge buffers both until the end of the step, they land milliseconds apart and the panel
always shows **0 s**. Append `tool/call` when the call arrives and `tool/result` when the result
arrives, settling the pending assistant message first so the visual order stays text-then-card.

### 5.5 Forward streaming deltas

If the bridge pushes the whole answer at the end of the step, the client cannot stream and
DSH's throughput figure becomes meaningless. Even when it does stream, that figure is
optimistic: the model reasons before the first visible token (458 tokens, 298 of them
reasoning, is not unusual), and DSH divides by the first-to-last visible chunk window.

---

## 6. DSH deployment notes

### 6.1 The web UI needs a token, and it changes on every restart

`dsh` generates a launch token with `randomBytes` and puts it in the printed URL, so the link
dies with the process. For a long-lived deployment:

- pin the token (our patch reads `DSH_WEB_TOKEN`);
- pin the advertised URL with `DSH_WEB_PUBLIC_URL`, otherwise a reverse proxy will advertise
  the internal port;
- `cookieMaxAgeDays: 365`, or phones will ask you to log in repeatedly.

### 6.2 `--trusted-host` has no effect in `--profile` mode

Symptom: the client reconnects forever and `/api/remote.mux` returns 403. In profile mode the
allow-list must live in the plugin layer:

```yaml
# cordis.patch.yml
- id: dsh-client-connection
  config:
    connection:
      trustedHosts: ["lab.example.com", "127.0.0.1:18766"]
```

### 6.3 A reverse proxy must forward the Host header verbatim

Use `proxy_set_header Host $http_host` (with the port). `$host` drops the port, DSH's origin
check fails, and you get the same 403.

---

## 7. Checklist

| Item | How to verify |
|---|---|
| Turns complete | send a message, check `turn/end` reason is `completed` |
| Long tools are not killed | ask for `sleep 210`, expect a normal result, not `MBOT_STALLED` |
| Approval round-trips | write outside the workspace, the DSH card must appear; test allow and deny |
| Background jobs report back | `background: true` for 45 s, **close the page**, reopen and expect a message |
| Tool timing is real | run `sleep 6`, the panel should show ~6 s, not 0 |
| Session survives idle | continue a conversation after 30 minutes, context should still be there |

---

## 8. Symptom → cause

| Symptom | Cause | Fix |
|---|---|---|
| Task stops halfway | `max_iterations` too small | raise to 300+ |
| Context gone after 30 min | checkpoint TTL wins over config | migrate both fields |
| Long command killed at 180 s | heartbeats not counted as progress | per-phase timeouts |
| `unhandled exception` from a tool | unbalanced quotes / dropped worker | catch and return a tool error |
| Approval never resolves | notification sent but no approval service | `ctx.reflect.get("approval")` |
| Client reconnects forever | trusted hosts / Host header lost the port | profile config + `$http_host` |
| Phones re-login constantly | 30-day cookie + rotating token | pin both |
| Heredoc returns nothing | wrapper did not add a newline | add it |
| Tool time always 0 s | events buffered to end of step | append at real timestamps |
