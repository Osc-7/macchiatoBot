"""Command line entrypoint for the lightweight local worker.

The first implementation intentionally focuses on packaging and local identity
configuration. Transport and sandbox execution will be wired in the next slice.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

CONFIG_PATH = Path.home() / ".config" / "macchiato" / "remote.json"


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


def _cmd_login(args: argparse.Namespace) -> int:
    data = _load_config()
    data.update(
        {
            "server": args.server.strip(),
            "login": args.login.strip(),
        }
    )
    _save_config(data)
    print(f"Saved remote worker login '{data['login']}' for {data['server']}")
    return 0


def _cmd_status(_: argparse.Namespace) -> int:
    data = _load_config()
    if not data:
        print("macchiato-remote is not configured. Run: macchiato-remote login")
        return 1
    print(f"server: {data.get('server') or '-'}")
    print(f"login: {data.get('login') or '-'}")
    print("transport: not connected (worker transport is not implemented yet)")
    return 0


def _cmd_start(_: argparse.Namespace) -> int:
    data = _load_config()
    if not data:
        print("macchiato-remote is not configured. Run: macchiato-remote login")
        return 1
    print(
        "Remote worker transport is not implemented yet; "
        "this command currently verifies packaging only."
    )
    return 2


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
    login.set_defaults(func=_cmd_login)

    status = sub.add_parser("status", help="show local worker configuration")
    status.set_defaults(func=_cmd_status)

    start = sub.add_parser("start", help="start the local remote worker")
    start.set_defaults(func=_cmd_start)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
