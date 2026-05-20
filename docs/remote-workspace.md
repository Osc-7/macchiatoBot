# Remote Workspaces

Remote workspace mode lets a macchiatoBot daemon operate on a folder that a user
authorized on another machine. The daemon keeps the LLM, memory, sessions,
Feishu, scheduling, and permission flow. The local machine runs only a lightweight
`macchiato-remote` worker for bash and file operations inside the authorized
workspace.

## Deployment Roles

| Role | Package | Runs where | Owns |
|---|---|---|---|
| Full bot | `macchiato-bot` | Cloud server, dev machine, or local full install | daemon, LLM, memory, frontends, scheduler, remote gateway |
| Worker only | `macchiato-remote` | Laptop, workstation, cluster node | authorized workspace access, local shell/file runtime |

A machine with the full bot can also run `macchiato-remote start` if it should
expose one of its folders to another daemon.

## Server Side

Start the daemon as usual:

```bash
uv run automation_daemon.py
# or, after installation:
macchiato-daemon
```

The daemon exposes the remote worker WebSocket gateway when remote mode is
enabled. Defaults:

| Setting | Default | Purpose |
|---|---|---|
| `MACCHIATO_REMOTE_HOST` | `0.0.0.0` | bind host |
| `MACCHIATO_REMOTE_PORT` | `9380` | bind port |
| `MACCHIATO_REMOTE_TOKENS` | empty | comma-separated `login=token` overrides |
| `MACCHIATO_REMOTE_TOKEN` | empty | shared fallback token |
| `MACCHIATO_REMOTE_LOGIN_BOOTSTRAP_TOKEN` | empty | bootstrap login exchange |
| `MACCHIATO_REMOTE_LOGIN_APPROVER_SECRET` | empty | manual approval panel secret |

The same port also serves:

- `GET /remote/healthz`
- `GET /remote/login`

## Install The Worker

Recommended worker-only install:

```bash
uv tool install macchiato-remote
macchiato-remote --version
```

For development from this repository:

```bash
uv sync
uv run macchiato-remote status
uv tool install -e packages/macchiato-remote
```

A worker-only machine does not need `config/config.yaml`, `.env`, Feishu config,
LLM keys, or the automation daemon.

## Configure A Login

Choose a stable login alias such as `personal`, `work-mbp`, or `studio-linux`.
This alias is the value used by `/remote-use`.

Generate a token on the daemon host:

```bash
uv run macchiato-remote gen-token --login personal
# optional: uv run macchiato-remote gen-token --login personal --bytes 48
```

The command stores only a sha256 digest in
`data/automation/remote_worker_tokens.json`. The plaintext token is printed once;
pass it to the worker:

```bash
macchiato-remote login   --server http://your-macchiato-server.example.com:9380   --login personal   --token '<paste gen-token output>'
```

You can also use positional server syntax:

```bash
macchiato-remote login your-macchiato-server.example.com:9380 --login personal
```

If `--token` is omitted, the CLI starts a device-login flow.

## Login Modes

Bootstrap exchange:

1. Set `MACCHIATO_REMOTE_LOGIN_BOOTSTRAP_TOKEN` on the server.
2. Run `macchiato-remote login <server> --login <alias> --auth-token '<bootstrap-token>'`.
3. The server verifies the bootstrap token and issues a worker token.
4. The CLI stores the worker token locally.

Manual approval panel:

1. The CLI requests a one-time code from `/remote/login/start`.
2. Open `/remote/login` and approve with `MACCHIATO_REMOTE_LOGIN_APPROVER_SECRET`.
3. The CLI polls `/remote/login/poll`, receives an onboarding token, and stores it.

Feishu approval:

1. Configure the server Feishu bot and `feishu.automation_activity_chat_id`.
2. Configure approver allowlists with `MACCHIATO_REMOTE_LOGIN_APPROVER_OPEN_IDS`
   and/or `MACCHIATO_REMOTE_LOGIN_APPROVER_USER_IDS`.
3. Run `macchiato-remote login <server-ip>:9380 --login <alias>`.
4. The server sends a Feishu approval card.
5. The CLI stores the token after approval.

## Run The Worker

Debug in the foreground:

```bash
macchiato-remote start
```

Daily background mode:

```bash
macchiato-remote start --background
macchiato-remote status
macchiato-remote stop
```

Background logs are written to `~/.local/state/macchiato/remote-worker.log`; the
pid file is `~/.local/state/macchiato/remote-worker.pid`.

## Use From CLI Or Feishu

After the worker is connected, switch only the current session into remote mode:

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
is active, `bash`, `read_file`, `write_file`, and `modify_file` are treated as
remote-workspace operations. `/workspace`, `~`, and relative paths refer to the
authorized local folder.

## Permission Profiles

| Profile | Intent |
|---|---|
| `strict` | Only the explicitly authorized workspace, minimal host exposure |
| `dev` | Developer-friendly sandbox with project access and common toolchain/cache mounts |
| `host-user` | Short-lived, user-confirmed access as the local OS user |
| `host-admin` | Highest-risk mode for explicit, per-command admin actions |

The default profile is `dev`. Higher-privilege modes should be short-lived and
audited; they are meant for explicit elevation, not as the default workspace.

## SSH Tunnel Mode

If public WebSocket access is unreliable, save an SSH tunnel configuration once:

```bash
macchiato-remote login   --server http://203.0.113.10:9380   --login macbook   --token '<same token as daemon>'   --ssh-tunnel ubuntu@203.0.113.10

macchiato-remote start --background
```

With `--ssh-tunnel` configured, `start`, `start --background`, and `probe` open
`127.0.0.1:19380 -> SSH_HOST:127.0.0.1:9380` automatically. Tune with
`--ssh-local-port`, `--ssh-remote-host`, and `--ssh-remote-port`; remove it with
`--clear-ssh-tunnel`.

## Troubleshooting

Run a handshake probe first:

```bash
macchiato-remote probe
```

If the first line is `HTTP/1.1 101 Switching Protocols`, TCP and WebSocket
upgrade are fine; look at the `websockets` stack and runtime environment.

If `probe` shows garbage/HTML or hangs, check whether Clash TUN, global VPN, or
a proxy chain is intercepting raw IP traffic. Emptying `http_proxy` in the shell
is not enough when TUN captures all traffic.

If Clash is off but `probe` times out, suspect stale routes or leftover `utun`
interfaces after VPN/TUN exit. Reboot, try another network, and re-check the
cloud security group for inbound TCP `9380`.

If traffic only works while Clash is on, the TUN exit may not have restored the
default route. Quit through the client normal path or reboot.

For daily use with TUN enabled, add a high-priority direct rule before the final
`MATCH` rule:

```yaml
rules:
  - IP-CIDR,203.0.113.10/32,DIRECT,no-resolve
```

Replace the IP with the daemon public IP, reload Clash, then run
`macchiato-remote probe` again.
