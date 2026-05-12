"""Command line entrypoint for the lightweight local worker.

The first implementation intentionally focuses on packaging and local identity
configuration. Transport and sandbox execution will be wired in the next slice.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from macchiato_remote.client import (RemoteWorkerClient,
                                     raw_websocket_handshake_probe)
from macchiato_remote.tokens import register_remote_worker_token

CONFIG_PATH = Path.home() / ".config" / "macchiato" / "remote.json"
STATE_DIR = Path.home() / ".local" / "state" / "macchiato"
PID_PATH = STATE_DIR / "remote-worker.pid"
LOG_PATH = STATE_DIR / "remote-worker.log"
DEFAULT_SSH_LOCAL_PORT = 19380


def _load_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _server_port(server: str) -> int:
    from urllib.parse import urlparse

    parsed = urlparse(server)
    if parsed.port:
        return int(parsed.port)
    return 443 if parsed.scheme == "https" else 80


def _default_ssh_remote_port(server: str) -> int:
    """Infer the remote daemon port behind SSH.

    If ``--server`` is a local tunnel endpoint (e.g. 127.0.0.1:19380), that
    port is local-only; the daemon behind SSH usually listens on 9380.
    """
    from urllib.parse import urlparse

    parsed = urlparse(server)
    host = (parsed.hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return 9380
    return _server_port(server)


def _local_tunnel_server_url(local_port: int) -> str:
    return f"http://127.0.0.1:{int(local_port)}"


def _read_pid() -> Optional[int]:
    try:
        raw = PID_PATH.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _pid_is_running(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def _current_executable() -> str:
    exe = Path(sys.argv[0])
    if exe.is_absolute() and exe.exists():
        return str(exe)
    found = shutil.which(exe.name)
    return found or str(exe)


def _wait_local_port(host: str, port: int, *, timeout_seconds: float = 10.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((host, int(port)), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _start_ssh_tunnel_if_configured(data: dict) -> Optional[subprocess.Popen[bytes]]:
    target = str(data.get("ssh_tunnel") or "").strip()
    if not target:
        return None

    server = str(data.get("server") or "").strip()
    remote_port = int(data.get("ssh_remote_port") or _server_port(server))
    remote_host = str(data.get("ssh_remote_host") or "127.0.0.1").strip()
    local_port = int(data.get("ssh_local_port") or DEFAULT_SSH_LOCAL_PORT)
    extra_raw = data.get("ssh_args") or []
    extra_args = [str(x) for x in extra_raw] if isinstance(extra_raw, list) else []

    cmd = [
        "ssh",
        "-N",
        "-L",
        f"{local_port}:{remote_host}:{remote_port}",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        *extra_args,
        target,
    ]
    print(
        "Starting SSH tunnel: "
        f"127.0.0.1:{local_port} -> {target}:{remote_host}:{remote_port}",
        file=sys.stderr,
        flush=True,
    )
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=sys.stderr,
        stderr=sys.stderr,
        start_new_session=False,
        close_fds=True,
    )
    if not _wait_local_port("127.0.0.1", local_port, timeout_seconds=10.0):
        if proc.poll() is not None:
            raise RuntimeError(f"SSH tunnel exited early with code {proc.returncode}")
        raise RuntimeError(f"SSH tunnel did not open local port {local_port}")
    print(
        f"SSH tunnel ready on http://127.0.0.1:{local_port}",
        file=sys.stderr,
        flush=True,
    )
    return proc


def _stop_ssh_tunnel(proc: Optional[subprocess.Popen[bytes]]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


def _start_background() -> int:
    data = _load_config()
    if not data:
        print("macchiato-remote is not configured. Run: macchiato-remote login")
        return 1

    old_pid = _read_pid()
    if _pid_is_running(old_pid):
        print(f"macchiato-remote is already running in background (pid={old_pid})")
        print(f"log: {LOG_PATH}")
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = LOG_PATH.open("a", encoding="utf-8")
    log_handle.write(
        f"\n--- macchiato-remote background start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
    )
    log_handle.flush()
    proc = subprocess.Popen(
        [_current_executable(), "start", "--foreground"],
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    log_handle.close()
    PID_PATH.write_text(f"{proc.pid}\n", encoding="utf-8")
    print(f"Started macchiato-remote in background (pid={proc.pid})")
    print(f"log: {LOG_PATH}")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    data = _load_config()
    ssh_tunnel = (args.ssh_tunnel or "").strip()
    data.update(
        {
            "server": args.server.strip(),
            "login": args.login.strip(),
            "token": (args.token or "").strip(),
        }
    )
    if ssh_tunnel:
        data.update(
            {
                "ssh_tunnel": ssh_tunnel,
                "ssh_local_port": int(args.ssh_local_port),
                "ssh_remote_host": args.ssh_remote_host.strip(),
                "ssh_remote_port": int(
                    args.ssh_remote_port or _default_ssh_remote_port(args.server)
                ),
            }
        )
    elif args.clear_ssh_tunnel:
        for key in (
            "ssh_tunnel",
            "ssh_local_port",
            "ssh_remote_host",
            "ssh_remote_port",
            "ssh_args",
        ):
            data.pop(key, None)
    _save_config(data)
    print(f"Saved remote worker login '{data['login']}' for {data['server']}")
    if ssh_tunnel:
        print(
            "Saved SSH tunnel: "
            f"127.0.0.1:{data['ssh_local_port']} -> "
            f"{data['ssh_tunnel']}:{data['ssh_remote_host']}:{data['ssh_remote_port']}"
        )
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    data = _load_config()
    if not data:
        print("macchiato-remote is not configured. Run: macchiato-remote login")
        return 1
    print(f"server: {data.get('server') or '-'}")
    print(f"login: {data.get('login') or '-'}")
    if data.get("ssh_tunnel"):
        print(
            "ssh_tunnel: "
            f"127.0.0.1:{data.get('ssh_local_port') or DEFAULT_SSH_LOCAL_PORT} -> "
            f"{data.get('ssh_tunnel')}:{data.get('ssh_remote_host') or '127.0.0.1'}:"
            f"{data.get('ssh_remote_port') or _server_port(str(data.get('server') or 'http://127.0.0.1:9380'))}"
        )
    if (data.get("token") or "").strip():
        print("token: configured (sent as ?token= on WebSocket)")
    else:
        print(
            "token: (empty — set with macchiato-remote login --token if server requires it)"
        )
    print(
        "transport: run `macchiato-remote start` on this machine while the cloud "
        "automation_daemon is up; worker connects outbound via WebSocket."
    )
    pid = _read_pid()
    if _pid_is_running(pid):
        print(f"background: running (pid={pid})")
        print(f"log: {LOG_PATH}")
    elif pid:
        print(f"background: not running (stale pid={pid})")
        print(f"log: {LOG_PATH}")
    else:
        print("background: not running")
    return 0


def _cmd_gen_token(args: argparse.Namespace) -> int:
    nbytes = max(16, int(args.bytes))
    login = str(getattr(args, "login", "") or "").strip()
    token = secrets.token_urlsafe(nbytes)
    registered_path: Optional[Path] = None
    if login and not bool(getattr(args, "no_register", False)):
        registered_path = register_remote_worker_token(
            login,
            token,
            path=str(getattr(args, "token_file", "") or "").strip() or None,
        )

    print(token)
    print()
    if login:
        if registered_path is not None:
            print("# 已注册到服务器 token 文件（只保存 sha256 摘要）：")
            print(f"#   {registered_path}")
            print("# daemon 每次 worker 握手都会读取该文件，通常无需改 systemd 环境变量。")
        else:
            print("# 未写入服务器 token 文件；可手动配置云上 daemon 环境变量：")
            print(f"#   export MACCHIATO_REMOTE_TOKENS='{login}={token}'")
    else:
        print("# 云上 daemon 与 systemd 环境变量示例（所有机器共用）：")
        print(f"#   export MACCHIATO_REMOTE_TOKEN='{token}'")
        print("# 多机器建议指定 --login，让服务器 token registry 自动记录。")
    print("# 本机写入 remote.json 时：")
    login_hint = login or "<别名>"
    print(
        f"#   macchiato-remote login --server <URL> --login {login_hint} --token '{token}'"
    )
    return 0


def _cmd_probe(_: argparse.Namespace) -> int:
    data = _load_config()
    if not data.get("server") or not data.get("login"):
        print(
            "macchiato-remote is not configured. Run: macchiato-remote login",
            file=sys.stderr,
        )
        return 1
    tunnel_proc: Optional[subprocess.Popen[bytes]] = None
    server = str(data.get("server") or "")
    if data.get("ssh_tunnel"):
        try:
            tunnel_proc = _start_ssh_tunnel_if_configured(data)
        except Exception as exc:
            print(f"probe failed to start SSH tunnel: {exc!r}", file=sys.stderr)
            return 2
        server = _local_tunnel_server_url(
            int(data.get("ssh_local_port") or DEFAULT_SSH_LOCAL_PORT)
        )
    try:
        text = raw_websocket_handshake_probe(
            server=server,
            login=str(data.get("login") or ""),
            token=str(data.get("token") or "").strip() or None,
        )
    except Exception as exc:
        print(f"probe failed: {exc!r}", file=sys.stderr)
        return 2
    finally:
        _stop_ssh_tunnel(tunnel_proc)
    print("--- raw HTTP response (first chunk, stdlib socket, ignores HTTP_PROXY) ---")
    if not text.strip():
        print(
            "(empty: peer closed TCP before sending any HTTP bytes — typical of "
            "Clash TUN / transparent filter, or a firewall dropping WebSocket upgrades.)"
        )
    else:
        print(text[:8000])
    if " 101 " in text.split("\r\n", 1)[0] or text.startswith("HTTP/1.1 101"):
        print(
            "\n(probe: server returned 101 — TCP + handshake path OK from this machine)"
        )
        return 0
    print(
        "\n(probe: expected status line HTTP/1.1 101; if you see HTML/TLS garbage, "
        "check Clash TUN / VPN or a middlebox on the path.)",
        file=sys.stderr,
    )
    return 3


def _cmd_start(args: argparse.Namespace) -> int:
    if bool(getattr(args, "background", False)):
        return _start_background()

    data = _load_config()
    if not data:
        print("macchiato-remote is not configured. Run: macchiato-remote login")
        return 1
    print(
        f"Starting macchiato-remote login='{data.get('login')}' "
        f"server='{data.get('server')}'"
    )
    try:
        asyncio.run(_run_worker(data))
    except KeyboardInterrupt:
        return 0
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    pid = _read_pid()
    if not pid:
        print("macchiato-remote background worker is not running (no pid file)")
        return 0
    if not _pid_is_running(pid):
        print(f"macchiato-remote background worker is not running (stale pid={pid})")
        try:
            PID_PATH.unlink()
        except OSError:
            pass
        return 0

    sig = signal.SIGKILL if bool(getattr(args, "force", False)) else signal.SIGTERM
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        print(f"macchiato-remote background worker is not running (stale pid={pid})")
        try:
            PID_PATH.unlink()
        except OSError:
            pass
        return 0
    except OSError:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            print(
                f"macchiato-remote background worker is not running (stale pid={pid})"
            )
            try:
                PID_PATH.unlink()
            except OSError:
                pass
            return 0

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not _pid_is_running(pid):
            break
        time.sleep(0.2)

    if _pid_is_running(pid):
        print(
            f"sent {sig.name} to macchiato-remote background worker (pid={pid}); "
            "process still appears alive"
        )
        return 1

    try:
        PID_PATH.unlink()
    except OSError:
        pass
    print(f"Stopped macchiato-remote background worker (pid={pid})")
    return 0


async def _run_worker(data: dict) -> None:
    tunnel_proc = _start_ssh_tunnel_if_configured(data)
    effective_server = str(data.get("server") or "")
    if data.get("ssh_tunnel"):
        effective_server = _local_tunnel_server_url(
            int(data.get("ssh_local_port") or DEFAULT_SSH_LOCAL_PORT)
        )
    client = RemoteWorkerClient(
        server=effective_server,
        login=str(data.get("login") or ""),
        token=str(data.get("token") or "") or None,
    )
    try:
        await client.run_forever()
    finally:
        await client.close()
        _stop_ssh_tunnel(tunnel_proc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="macchiato-remote",
        description="Lightweight local worker for macchiatoBot remote workspaces.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="save the server URL and login alias")
    login.add_argument("--server", required=True, help="macchiatoBot server URL")
    login.add_argument(
        "--login",
        required=True,
        help="login alias used by /remote-use, e.g. personal or work-mbp",
    )
    login.add_argument(
        "--token",
        default="",
        help=(
            "optional worker token; must match MACCHIATO_REMOTE_TOKENS entry or "
            "MACCHIATO_REMOTE_TOKEN on the daemon "
            "(generate one: macchiato-remote gen-token)"
        ),
    )
    login.add_argument(
        "--ssh-tunnel",
        default="",
        metavar="USER@HOST",
        help=(
            "optional SSH target; start/start --background will automatically "
            "open -L local:remote before connecting"
        ),
    )
    login.add_argument(
        "--ssh-local-port",
        type=int,
        default=DEFAULT_SSH_LOCAL_PORT,
        help=f"local tunnel port (default: {DEFAULT_SSH_LOCAL_PORT})",
    )
    login.add_argument(
        "--ssh-remote-host",
        default="127.0.0.1",
        help="remote bind target seen from SSH server (default: 127.0.0.1)",
    )
    login.add_argument(
        "--ssh-remote-port",
        type=int,
        default=0,
        help="remote port to forward to (default: port from --server)",
    )
    login.add_argument(
        "--clear-ssh-tunnel",
        action="store_true",
        help="remove previously saved SSH tunnel settings",
    )
    login.set_defaults(func=_cmd_login)

    gen = sub.add_parser(
        "gen-token",
        help="print a URL-safe random token for MACCHIATO_REMOTE_TOKEN / login --token",
    )
    gen.add_argument(
        "--bytes",
        type=int,
        default=32,
        metavar="N",
        help="entropy size in bytes before base64-url encoding (default: 32, min: 16)",
    )
    gen.add_argument(
        "--login",
        default="",
        help=(
            "optional login alias; when provided, register this machine token "
            "in the server token registry"
        ),
    )
    gen.add_argument(
        "--no-register",
        action="store_true",
        help="print only; do not update the server token registry",
    )
    gen.add_argument(
        "--token-file",
        default="",
        help=(
            "server token registry path "
            "(default: data/automation/remote_worker_tokens.json, or "
            "MACCHIATO_REMOTE_TOKEN_FILE)"
        ),
    )
    gen.set_defaults(func=_cmd_gen_token)

    status = sub.add_parser("status", help="show local worker configuration")
    status.set_defaults(func=_cmd_status)

    probe = sub.add_parser(
        "probe",
        help="raw TCP WebSocket handshake test (bypasses asyncio/HTTP_PROXY; uses remote.json)",
    )
    probe.set_defaults(func=_cmd_probe)

    start = sub.add_parser("start", help="start the local remote worker")
    start.add_argument(
        "--background",
        "-b",
        action="store_true",
        help="start the worker as a detached background process",
    )
    start.add_argument(
        "--foreground",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    start.set_defaults(func=_cmd_start)

    stop = sub.add_parser("stop", help="stop the background remote worker")
    stop.add_argument(
        "--force",
        action="store_true",
        help="send SIGKILL instead of SIGTERM",
    )
    stop.set_defaults(func=_cmd_stop)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
