"""Session-scoped stdio MCP host for the remote worker.

Keep this module free of ``agent_core`` imports.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from macchiato_remote.runtime.mcp_config import (
    WorkspaceMcpServerConfig,
    find_server,
    load_workspace_mcp_config,
)

logger = logging.getLogger(__name__)


@dataclass
class _ServerRuntime:
    config: WorkspaceMcpServerConfig
    session: Any
    stop_event: asyncio.Event
    runner_task: asyncio.Task[None]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tool_metas: List[Tuple[str, str, Dict[str, Any]]] = field(default_factory=list)

    def is_alive(self) -> bool:
        return not self.runner_task.done()


class RemoteMcpHost:
    """Manage MCP stdio sessions keyed by ``(session_id, server_name)``."""

    def __init__(self) -> None:
        self._runtimes: Dict[Tuple[str, str], _ServerRuntime] = {}
        self._workspace_roots: Dict[str, Path] = {}

    def bind_workspace(self, session_id: str, workspace_root: Path | str) -> None:
        self._workspace_roots[session_id] = Path(workspace_root).expanduser().resolve()

    def unbind_workspace(self, session_id: str) -> None:
        self._workspace_roots.pop(session_id, None)

    def workspace_root(self, session_id: str) -> Optional[Path]:
        return self._workspace_roots.get(session_id)

    async def ensure(
        self, session_id: str, server_names: List[str]
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        root = self._workspace_roots.get(session_id)
        if root is None:
            for name in server_names:
                results.append(
                    {
                        "name": name,
                        "ok": False,
                        "error": "SESSION_NOT_OPEN",
                    }
                )
            return results

        try:
            from mcp import ClientSession  # noqa: F401
        except ImportError:
            for name in server_names:
                results.append(
                    {
                        "name": name,
                        "ok": False,
                        "error": "MCP_SDK_MISSING",
                    }
                )
            return results

        try:
            cfg = load_workspace_mcp_config(root)
        except Exception as exc:
            for name in server_names:
                results.append(
                    {
                        "name": name,
                        "ok": False,
                        "error": f"MCP_CONFIG_ERROR:{exc}",
                    }
                )
            return results

        if not (root / ".macchiato" / "mcp.yaml").is_file():
            for name in server_names:
                results.append(
                    {
                        "name": name,
                        "ok": False,
                        "error": "MCP_CONFIG_MISSING",
                    }
                )
            return results

        for raw_name in server_names:
            name = (raw_name or "").strip()
            key = (session_id, name)
            existing = self._runtimes.get(key)
            if existing is not None:
                if existing.is_alive():
                    results.append({"name": name, "ok": True, "error": None})
                    continue
                self._runtimes.pop(key, None)
                await self._close_runtime(key[0], key[1], existing)
            server = find_server(cfg, name)
            if server is None:
                results.append(
                    {"name": name, "ok": False, "error": "MCP_SERVER_NOT_FOUND"}
                )
                continue
            if not server.enabled:
                results.append(
                    {"name": name, "ok": False, "error": "MCP_SERVER_DISABLED"}
                )
                continue
            if server.transport != "stdio":
                results.append(
                    {"name": name, "ok": False, "error": "MCP_TRANSPORT_UNSUPPORTED"}
                )
                continue
            try:
                runtime = await self._connect_stdio(
                    server,
                    workspace_root=root,
                    session_id=session_id,
                )
            except Exception as exc:
                logger.warning(
                    "remote mcp ensure failed session=%s server=%s: %s",
                    session_id,
                    name,
                    exc,
                )
                results.append(
                    {
                        "name": name,
                        "ok": False,
                        "error": f"MCP_CONNECT_FAILED:{type(exc).__name__}",
                    }
                )
                continue
            self._runtimes[key] = runtime
            results.append({"name": name, "ok": True, "error": None})
        return results

    async def list_tools(
        self,
        session_id: str,
        server_name: str,
        *,
        refresh: bool = False,
    ) -> Dict[str, Any]:
        name = (server_name or "").strip()
        key = (session_id, name)
        runtime = self._runtimes.get(key)
        if runtime is not None and not runtime.is_alive():
            self._runtimes.pop(key, None)
            await self._close_runtime(session_id, name, runtime)
            runtime = None
        if runtime is None:
            ensure = await self.ensure(session_id, [name])
            status = ensure[0] if ensure else {"ok": False, "error": "MCP_SERVER_NOT_FOUND"}
            if not status.get("ok"):
                return {
                    "server_name": name,
                    "tools": [],
                    "error": status.get("error") or "MCP_SERVER_NOT_FOUND",
                }
            runtime = self._runtimes.get(key)
        if runtime is None:
            return {
                "server_name": name,
                "tools": [],
                "error": "MCP_SERVER_NOT_FOUND",
            }

        if refresh or not runtime.tool_metas:
            try:
                tool_resp = await asyncio.wait_for(
                    runtime.session.list_tools(),
                    timeout=runtime.config.init_timeout_seconds,
                )
            except Exception as exc:
                return {
                    "server_name": name,
                    "tools": [],
                    "error": f"MCP_LIST_FAILED:{type(exc).__name__}:{exc}",
                }
            metas: List[Tuple[str, str, Dict[str, Any]]] = []
            for tool in getattr(tool_resp, "tools", []) or []:
                remote_name = getattr(tool, "name", "") or ""
                if not remote_name:
                    continue
                schema = (
                    getattr(tool, "inputSchema", None)
                    or getattr(tool, "input_schema", None)
                    or {"type": "object", "properties": {}}
                )
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                metas.append(
                    (
                        remote_name,
                        getattr(tool, "description", "") or "",
                        schema,
                    )
                )
            runtime.tool_metas = metas

        return {
            "server_name": name,
            "tools": [
                {
                    "name": n,
                    "description": d,
                    "input_schema": s,
                }
                for n, d, s in runtime.tool_metas
            ],
            "error": None,
        }

    async def call_tool(
        self,
        session_id: str,
        server_name: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        name = (server_name or "").strip()
        key = (session_id, name)
        runtime = self._runtimes.get(key)
        if runtime is not None and not runtime.is_alive():
            self._runtimes.pop(key, None)
            await self._close_runtime(session_id, name, runtime)
            runtime = None
        if runtime is None:
            return {
                "is_error": True,
                "content": [],
                "structured_content": None,
                "error": "MCP_SERVER_NOT_FOUND",
            }
        timeout = timeout_seconds or float(runtime.config.call_timeout_seconds)
        args = arguments if isinstance(arguments, dict) else {}
        try:
            async with runtime.lock:
                result = await asyncio.wait_for(
                    runtime.session.call_tool(tool_name, arguments=args),
                    timeout=timeout,
                )
        except asyncio.TimeoutError:
            return {
                "is_error": True,
                "content": [],
                "structured_content": None,
                "error": "MCP_TOOL_TIMEOUT",
            }
        except Exception as exc:
            return {
                "is_error": True,
                "content": [],
                "structured_content": None,
                "error": f"MCP_TOOL_CALL_FAILED:{exc}",
            }

        is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
        content_blocks: List[Dict[str, Any]] = []
        for block in getattr(result, "content", None) or []:
            if hasattr(block, "model_dump"):
                content_blocks.append(block.model_dump())
            elif isinstance(block, dict):
                content_blocks.append(block)
            else:
                content_blocks.append({"type": "text", "text": str(block)})
        structured = getattr(result, "structuredContent", None) or getattr(
            result, "structured_content", None
        )
        if structured is not None and hasattr(structured, "model_dump"):
            structured = structured.model_dump()
        return {
            "is_error": is_error,
            "content": content_blocks,
            "structured_content": structured if isinstance(structured, dict) else None,
            "error": None if not is_error else "MCP_TOOL_RETURNED_ERROR",
        }

    async def _close_runtime(
        self,
        session_id: str,
        server_name: str,
        runtime: _ServerRuntime,
    ) -> None:
        runtime.stop_event.set()
        try:
            await asyncio.wait_for(runtime.runner_task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(
                "remote mcp shutdown timed out session=%s server=%s; cancelling task",
                session_id,
                server_name,
            )
            runtime.runner_task.cancel()
            await asyncio.gather(runtime.runner_task, return_exceptions=True)
        except Exception:
            logger.exception(
                "remote mcp shutdown error session=%s server=%s",
                session_id,
                server_name,
            )

    async def shutdown(
        self, session_id: str, server_name: Optional[str] = None
    ) -> List[str]:
        closed: List[str] = []
        target = (server_name or "").strip() or None
        keys = [
            key
            for key in list(self._runtimes)
            if key[0] == session_id and (target is None or key[1] == target)
        ]
        for key in keys:
            runtime = self._runtimes.pop(key, None)
            if runtime is None:
                continue
            await self._close_runtime(key[0], key[1], runtime)
            closed.append(key[1])
        if target is None:
            self.unbind_workspace(session_id)
        return closed

    async def shutdown_all(self) -> List[Tuple[str, str]]:
        closed: List[Tuple[str, str]] = []
        for key in list(self._runtimes):
            runtime = self._runtimes.pop(key, None)
            if runtime is None:
                continue
            await self._close_runtime(key[0], key[1], runtime)
            closed.append(key)
        self._workspace_roots.clear()
        return closed

    async def _connect_stdio(
        self,
        server: WorkspaceMcpServerConfig,
        *,
        workspace_root: Path,
        session_id: str = "",
    ) -> _ServerRuntime:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        cwd = server.cwd
        if cwd is None or str(cwd).strip() in {"", ".", "null"}:
            cwd_path = str(workspace_root)
        else:
            cwd_path = str(Path(cwd).expanduser())
            if not Path(cwd_path).is_absolute():
                cwd_path = str((workspace_root / cwd_path).resolve())

        merged_env = {**os.environ, **(server.env or {})}
        merged_env.setdefault("NODE_NO_WARNINGS", "1")
        merged_env.setdefault("NODE_ENV", "production")

        params = StdioServerParameters(
            command=server.command,
            args=list(server.args or []),
            env=merged_env,
            cwd=cwd_path,
        )
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[Any] = loop.create_future()
        stop_event = asyncio.Event()

        async def _lifecycle() -> None:
            try:
                async with AsyncExitStack() as stack:
                    read, write = await stack.enter_async_context(stdio_client(params))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await asyncio.wait_for(
                        session.initialize(),
                        timeout=server.init_timeout_seconds,
                    )
                    if not ready.done():
                        ready.set_result(session)
                    await stop_event.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                    return
                if not isinstance(exc, asyncio.CancelledError) and not stop_event.is_set():
                    logger.warning(
                        "remote mcp runtime exited session=%s server=%s: %s",
                        session_id,
                        server.name,
                        exc,
                    )

        task_label = f"mcp-stdio-{session_id}:{server.name}" if session_id else f"mcp-stdio-{server.name}"
        runner_task = asyncio.create_task(_lifecycle(), name=task_label)
        try:
            session = await ready
        except BaseException:
            stop_event.set()
            runner_task.cancel()
            await asyncio.gather(runner_task, return_exceptions=True)
            raise
        return _ServerRuntime(
            config=server,
            session=session,
            stop_event=stop_event,
            runner_task=runner_task,
        )
