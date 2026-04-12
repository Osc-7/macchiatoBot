"""
进程内共享的外部 MCP（stdio）连接池。

同一「启动参数等价」的 MCP Server 只保留一条子进程 + Session，多 AgentCore 引用计数复用。
分桶键为 transport + command + args + cwd + env（见 mcp_pool_identity_key），
因此在 yaml 里新增任意 stdio MCP（npx、uvx、自定义命令等），只要配置相同即自动共用一套进程。

本地 schedule_tools（mcp_server.py）不参与池化；内置日程能力由 Agent 进程内 ToolRegistry 提供，
无需为每个 AgentCore 再起 mcp_server.py（参见 MCPConfig.inject_builtin_schedule_mcp）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from agent_core.config import MCPServerConfig

from .stdio_transport import MCPServerRuntime, connect_stdio_mcp_with_retries

logger = logging.getLogger(__name__)


def should_pool_stdio_server(server: MCPServerConfig) -> bool:
    """是否可对 stdio MCP 使用进程内共享池（排除本地日程 MCP 子进程）。"""
    if server.transport != "stdio":
        return False
    if server.name == "schedule_tools":
        return False
    if any("mcp_server.py" in str(arg) for arg in server.args):
        return False
    return True


def mcp_pool_identity_key(server: MCPServerConfig) -> str:
    """
    用于池分桶的稳定身份键（不含 tool_name_prefix / name / 超时等纯客户端策略字段）。
    配置等价的两个 server 会共用同一子进程。
    """
    payload = {
        "transport": server.transport,
        "command": server.command,
        "args": list(server.args),
        "cwd": server.cwd or "",
        "env": sorted((server.env or {}).items()),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


ToolMeta = Tuple[str, str, Dict[str, Any]]  # remote_name, description, input_schema


@dataclass
class _PooledEntry:
    stack: Any
    runtime: MCPServerRuntime
    tool_metas: List[ToolMeta]
    refcount: int = 0
    call_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SharedMCPPool:
    """全局（进程内）外部 MCP stdio 连接池。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: Dict[str, _PooledEntry] = {}

    async def acquire(
        self, server: MCPServerConfig
    ) -> Optional[Tuple[str, MCPServerRuntime, List[ToolMeta]]]:
        """引用 +1；首次命中桶时拉起子进程并 list_tools。失败返回 None。"""
        key = mcp_pool_identity_key(server)
        async with self._lock:
            if key not in self._entries:
                connected = await connect_stdio_mcp_with_retries(server)
                if connected is None:
                    return None
                stack, runtime = connected
                try:
                    tool_resp = await asyncio.wait_for(
                        runtime.session.list_tools(),
                        timeout=server.init_timeout_seconds,
                    )
                except Exception as exc:
                    logger.warning(
                        "MCP pool list_tools failed for %s (%s): %s",
                        server.name,
                        key[:12],
                        exc,
                    )
                    await stack.aclose()
                    return None

                tools = getattr(tool_resp, "tools", []) or []
                metas: List[ToolMeta] = []
                for tool in tools:
                    remote_name = getattr(tool, "name", "")
                    if not remote_name:
                        continue
                    metas.append(
                        (
                            remote_name,
                            getattr(tool, "description", "") or "MCP 远程工具",
                            getattr(tool, "inputSchema", None)
                            or getattr(tool, "input_schema", None)
                            or {"type": "object", "properties": {}},
                        )
                    )

                self._entries[key] = _PooledEntry(
                    stack=stack, runtime=runtime, tool_metas=metas
                )
                logger.info(
                    "MCP pool: new shared connection %s… (server=%s, tools=%d)",
                    key[:12],
                    server.name,
                    len(metas),
                )

            entry = self._entries[key]
            entry.refcount += 1
            return key, entry.runtime, list(entry.tool_metas)

    async def release(self, key: str) -> None:
        """引用 -1；归零时关闭子进程。"""
        to_close = None
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.refcount -= 1
            if entry.refcount <= 0:
                to_close = entry.stack
                del self._entries[key]
                logger.info("MCP pool: closed shared connection %s…", key[:12])
        if to_close is not None:
            await to_close.aclose()

    @asynccontextmanager
    async def locked_session_call(self, key: str):
        """对共享 Session 的 call_tool 串行化，降低并发协议风险。"""
        async with self._lock:
            entry = self._entries.get(key)
            call_lock = entry.call_lock if entry else None
        if call_lock is not None:
            async with call_lock:
                yield
        else:
            yield


_shared_mcp_pool: Optional[SharedMCPPool] = None


def get_shared_mcp_pool() -> SharedMCPPool:
    global _shared_mcp_pool
    if _shared_mcp_pool is None:
        _shared_mcp_pool = SharedMCPPool()
    return _shared_mcp_pool


def reset_shared_mcp_pool_for_tests() -> None:
    """测试用：清空全局池（仅应在无并发连接时调用）。"""
    global _shared_mcp_pool
    _shared_mcp_pool = None
