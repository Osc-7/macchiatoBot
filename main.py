#!/usr/bin/env python3
"""macchiatoBot CLI compatibility shim for checkout / ``uv run``.

The implementation lives in :mod:`macchiato_bot_cli.main`.  The root module
keeps legacy imports and test monkeypatching working while still using the
packaged entrypoint code.
"""

from macchiato_bot_cli import main as _cli

AutomationIPCClient = _cli.AutomationIPCClient
default_socket_path = _cli.default_socket_path
get_config = _cli.get_config
run_interactive_loop = _cli.run_interactive_loop
run_single_command = _cli.run_single_command


async def main_async(args=None):
    """Delegate to the packaged CLI while honoring monkeypatched shim globals."""
    _cli.AutomationIPCClient = AutomationIPCClient
    _cli.default_socket_path = default_socket_path
    _cli.get_config = get_config
    _cli.run_interactive_loop = run_interactive_loop
    _cli.run_single_command = run_single_command
    return await _cli.main_async(args)


def main():
    """Run the packaged CLI entrypoint through the compatibility wrapper."""
    _cli.main_async = main_async
    return _cli.main()


if __name__ == "__main__":
    main()
