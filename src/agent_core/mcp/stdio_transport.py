"""
MCP stdio 传输：建立子进程 + ClientSession（供 MCPClientManager 与共享池复用）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from agent_core.config import MCPServerConfig

logger = logging.getLogger(__name__)


@dataclass
class MCPServerRuntime:
    """单个 MCP Server 的运行时句柄（stdio 会话）。"""

    config: MCPServerConfig
    session: Any


async def connect_stdio_mcp_with_retries(
    server: MCPServerConfig,
) -> Optional[Tuple[AsyncExitStack, MCPServerRuntime]]:
    """
    启动 stdio MCP 子进程并完成 initialize。

    失败（含重试耗尽）时返回 None；成功返回 (exit_stack, runtime)，栈由调用方负责关闭。
    """
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as e:
        raise RuntimeError(
            "未安装 mcp 依赖，请先执行: source init.sh（或 uv sync --all-groups）"
        ) from e

    args_preview = " ".join(server.args)
    if len(args_preview) > 60:
        args_preview = args_preview[:57] + "..."
    max_attempts = 1 + getattr(server, "init_retries", 0)
    retry_delay = getattr(server, "init_retry_delay_seconds", 2.0)

    for attempt in range(max_attempts):
        if attempt > 0:
            logger.info(
                "MCP server %s retry %s/%s in %.1fs...",
                server.name,
                attempt + 1,
                max_attempts,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
        else:
            logger.info(
                "Connecting to MCP server: %s (%s %s)...",
                server.name,
                server.command,
                args_preview,
            )

        attempt_stack: Optional[AsyncExitStack] = None
        try:
            merged_env = {**os.environ, **(server.env or {})}
            merged_env["NODE_NO_WARNINGS"] = "1"
            merged_env["NODE_ENV"] = "production"

            server_params = StdioServerParameters(
                command=server.command,
                args=server.args,
                env=merged_env,
                cwd=server.cwd,
            )

            attempt_stack = AsyncExitStack()
            await attempt_stack.__aenter__()

            if server.command == "npx" and any(
                arg == "mcp-remote" for arg in server.args
            ):
                errlog = attempt_stack.enter_context(open(os.devnull, "w"))
            else:
                errlog = sys.stderr

            read_stream, write_stream = await attempt_stack.enter_async_context(
                stdio_client(server_params, errlog=errlog)
            )
            session = await attempt_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )

            await asyncio.wait_for(
                session.initialize(),
                timeout=server.init_timeout_seconds,
            )

            runtime = MCPServerRuntime(config=server, session=session)
            return attempt_stack, runtime

        except Exception as exc:
            if attempt_stack is not None:
                await attempt_stack.aclose()
            if attempt < max_attempts - 1:
                logger.warning(
                    "MCP server %s attempt %s failed (will retry): %s (%s)",
                    server.name,
                    attempt + 1,
                    type(exc).__name__,
                    exc,
                )
            else:
                logger.warning(
                    "MCP server %s failed after %s attempt(s) (skipping): %s (%s)",
                    server.name,
                    max_attempts,
                    type(exc).__name__,
                    exc,
                )
    return None
