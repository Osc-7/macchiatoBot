#!/usr/bin/env python3
"""Automation worker entrypoint.

Runs scheduler + event handlers for background sync/summary/notification workflows.
"""

import asyncio
import signal

from schedule_agent.automation.runtime import get_runtime


async def _main() -> None:
    runtime = await get_runtime()
    await runtime.start(start_scheduler=True)

    stop_event = asyncio.Event()

    def _stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    await stop_event.wait()
    await runtime.stop()


if __name__ == "__main__":
    asyncio.run(_main())
