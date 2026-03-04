"""Local IPC bridge for long-running automation process."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from schedule_agent.core.interfaces import AgentHooks, AgentRunInput, AgentRunResult, InjectMessageCommand

from .core_gateway import AutomationCoreGateway

logger = logging.getLogger(__name__)


def default_socket_path() -> str:
    test_dir = os.environ.get("SCHEDULE_AGENT_TEST_DATA_DIR")
    if test_dir:
        return str(Path(test_dir) / "automation" / "automation.sock")
    return str(Path("data") / "automation" / "automation.sock")


@dataclass
class IPCServerPolicy:
    expire_check_interval_seconds: int = 60


class AutomationIPCServer:
    """JSON-RPC-like unix socket server for driving AutomationCoreGateway."""

    def __init__(
        self,
        gateway: AutomationCoreGateway,
        *,
        owner_id: str = "root",
        source: str = "cli",
        socket_path: Optional[str] = None,
        policy: Optional[IPCServerPolicy] = None,
    ) -> None:
        self._gateway = gateway
        self._owner_id = owner_id.strip() or "root"
        self._source = source.strip() or "cli"
        self._socket_path = socket_path or default_socket_path()
        self._policy = policy or IPCServerPolicy()
        self._server: Optional[asyncio.base_events.Server] = None
        self._expire_task: Optional[asyncio.Task[Any]] = None
        self._stopped = asyncio.Event()
        self._client_active_session: Dict[str, str] = {}

    @property
    def socket_path(self) -> str:
        return self._socket_path

    async def start(self) -> None:
        path = Path(self._socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(path))
        self._expire_task = asyncio.create_task(self._expire_loop(), name="automation-ipc-expire")

    async def stop(self) -> None:
        self._stopped.set()
        if self._expire_task is not None:
            self._expire_task.cancel()
            await asyncio.gather(self._expire_task, return_exceptions=True)
            self._expire_task = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        path = Path(self._socket_path)
        if path.exists():
            path.unlink()

    async def _expire_loop(self) -> None:
        interval = max(5, int(self._policy.expire_check_interval_seconds))
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(asyncio.shield(self._stopped.wait()), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            try:
                for sid in self._gateway.list_sessions():
                    if self._gateway.should_expire_session(session_id=sid):
                        await self._gateway.expire_session(reason="timer", session_id=sid)
            except Exception as exc:
                logger.warning("automation ipc expire loop failed: %s", exc)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                req_id = None
                try:
                    req = json.loads(raw.decode("utf-8"))
                    req_id = req.get("id")
                    method = str(req.get("method") or "")
                    params = req.get("params") or {}
                    if method == "run_turn_stream":
                        await self._handle_run_turn_stream(req_id, params, writer)
                        continue
                    result = await self._dispatch(method, params)
                    payload = {"id": req_id, "ok": True, "result": result}
                except Exception as exc:
                    payload = {"id": req_id, "ok": False, "error": str(exc)}
                writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            if peer is not None:
                self._client_active_session.pop(str(peer), None)

    async def _handle_run_turn_stream(
        self,
        req_id: Any,
        params: Dict[str, Any],
        writer: asyncio.StreamWriter,
    ) -> None:
        client_id = str(params.get("client_id") or "default")
        if client_id not in self._client_active_session:
            self._client_active_session[client_id] = f"{self._source}:default"
        active_session = self._client_active_session[client_id]
        text = str(params.get("text") or "")
        metadata = params.get("metadata")
        trace_events: list[dict[str, Any]] = []

        async def _send_event(event_type: str, payload: Dict[str, Any]) -> None:
            line = {"id": req_id, "stream": True, "event": event_type, **payload}
            writer.write((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()

        async def _on_trace_event(evt: Dict[str, Any]) -> None:
            trace_events.append(evt)
            await _send_event("trace", {"data": evt})

        async def _on_assistant_delta(delta: str) -> None:
            if not delta:
                return
            await _send_event("assistant_delta", {"delta": delta})

        async def _on_reasoning_delta(delta: str) -> None:
            if not delta:
                return
            await _send_event("reasoning_delta", {"delta": delta})

        hooks = AgentHooks(
            on_trace_event=_on_trace_event,
            on_assistant_delta=_on_assistant_delta,
            on_reasoning_delta=_on_reasoning_delta,
        )

        try:
            result = await self._gateway.inject_message(
                InjectMessageCommand(
                    session_id=active_session,
                    input=AgentRunInput(text=text, metadata=metadata if isinstance(metadata, dict) else None),
                ),
                hooks=hooks,
            )
            usage = self._gateway.get_token_usage(session_id=active_session)
            turn_count = self._gateway.get_turn_count(session_id=active_session)
            await _send_event(
                "final",
                {
                    "ok": True,
                    "result": {
                        "output_text": result.output_text,
                        "metadata": result.metadata,
                        "trace_events": trace_events,
                        "token_usage": usage,
                        "turn_count": turn_count,
                    },
                },
            )
        except Exception as exc:
            await _send_event("final", {"ok": False, "error": str(exc)})

    async def _dispatch(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        client_id = str(params.get("client_id") or "default")
        if client_id not in self._client_active_session:
            self._client_active_session[client_id] = f"{self._source}:default"
        active_session = self._client_active_session[client_id]

        if method == "ping":
            return {"status": "ok"}

        if method == "session_get":
            return {
                "owner_id": self._owner_id,
                "source": self._source,
                "active_session_id": active_session,
            }

        if method == "session_list":
            return {"sessions": self._gateway.list_sessions(), "active_session_id": active_session}

        if method == "session_switch":
            session_id = str(params.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("session_id 不能为空")
            create_if_missing = bool(params.get("create_if_missing", True))
            created = await self._gateway.ensure_session(session_id, create_if_missing=create_if_missing)
            self._client_active_session[client_id] = session_id
            self._gateway.mark_activity(session_id)
            return {"created": created, "active_session_id": session_id}

        if method == "clear_context":
            await self._gateway.clear_context_for_session(active_session)
            return {"ok": True}

        if method == "get_token_usage":
            usage = self._gateway.get_token_usage(session_id=active_session)
            return {"usage": usage}

        if method == "get_turn_count":
            turn_count = self._gateway.get_turn_count(session_id=active_session)
            return {"turn_count": turn_count}

        if method == "run_turn":
            text = str(params.get("text") or "")
            metadata = params.get("metadata")
            trace_events: list[dict[str, Any]] = []

            async def _on_trace_event(evt: Dict[str, Any]) -> None:
                trace_events.append(evt)

            hooks = AgentHooks(on_trace_event=_on_trace_event)
            result = await self._gateway.inject_message(
                InjectMessageCommand(
                    session_id=active_session,
                    input=AgentRunInput(text=text, metadata=metadata if isinstance(metadata, dict) else None),
                ),
                hooks=hooks,
            )
            usage = self._gateway.get_token_usage(session_id=active_session)
            turn_count = self._gateway.get_turn_count(session_id=active_session)
            return {
                "output_text": result.output_text,
                "metadata": result.metadata,
                "trace_events": trace_events,
                "token_usage": usage,
                "turn_count": turn_count,
            }

        raise ValueError(f"unknown method: {method}")


class AutomationIPCClient:
    """Async client for AutomationIPCServer."""

    def __init__(
        self,
        *,
        owner_id: str = "root",
        source: str = "cli",
        socket_path: Optional[str] = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.owner_id = owner_id.strip() or "root"
        self.source = source.strip() or "cli"
        self.active_session_id = f"{self.source}:default"
        self._socket_path = socket_path or default_socket_path()
        self._timeout_seconds = float(timeout_seconds)
        self._token_usage_cache: Dict[str, Any] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "call_count": 0,
            "cost_yuan": 0.0,
        }
        self._turn_count_cache = 0
        self._client_id = f"{os.getpid()}-{id(self)}"

    @property
    def config(self) -> Any:
        return None

    async def connect(self) -> None:
        data = await self._request("session_get", {})
        self.owner_id = str(data.get("owner_id") or self.owner_id)
        self.source = str(data.get("source") or self.source)
        self.active_session_id = str(data.get("active_session_id") or self.active_session_id)

    async def close(self) -> None:
        return

    async def ping(self) -> bool:
        try:
            await self._request("ping", {})
            return True
        except Exception:
            return False

    async def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self._socket_path),
            timeout=self._timeout_seconds,
        )
        req = {
            "id": f"{self._client_id}:{method}",
            "method": method,
            "params": {"client_id": self._client_id, **params},
        }
        try:
            writer.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), timeout=self._timeout_seconds)
        finally:
            writer.close()
            await writer.wait_closed()
        if not raw:
            raise RuntimeError("empty response from automation ipc server")
        payload = json.loads(raw.decode("utf-8"))
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error") or "automation ipc error"))
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    async def list_sessions(self) -> list[str]:
        data = await self._request("session_list", {})
        self.active_session_id = str(data.get("active_session_id") or self.active_session_id)
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            return []
        return [str(s) for s in sessions]

    async def switch_session(self, session_id: str, *, create_if_missing: bool = True) -> bool:
        data = await self._request(
            "session_switch",
            {"session_id": session_id, "create_if_missing": create_if_missing},
        )
        self.active_session_id = str(data.get("active_session_id") or session_id)
        return bool(data.get("created", False))

    async def clear_context(self) -> None:
        await self._request("clear_context", {})

    async def get_token_usage(self) -> dict:
        data = await self._request("get_token_usage", {})
        usage = data.get("usage")
        if isinstance(usage, dict):
            self._token_usage_cache = usage
        return dict(self._token_usage_cache)

    async def get_turn_count(self) -> int:
        data = await self._request("get_turn_count", {})
        try:
            self._turn_count_cache = int(data.get("turn_count", 0))
        except Exception:
            self._turn_count_cache = 0
        return self._turn_count_cache

    async def run_turn(self, agent_input: AgentRunInput, hooks: AgentHooks | None = None) -> AgentRunResult:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self._socket_path),
            timeout=self._timeout_seconds,
        )
        req = {
            "id": f"{self._client_id}:run_turn_stream",
            "method": "run_turn_stream",
            "params": {
                "client_id": self._client_id,
                "text": agent_input.text,
                "metadata": agent_input.metadata,
            },
        }
        writer.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

        final_result: Optional[Dict[str, Any]] = None
        try:
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=self._timeout_seconds)
                if not raw:
                    break
                payload = json.loads(raw.decode("utf-8"))
                if not payload.get("stream"):
                    continue
                event_type = str(payload.get("event") or "")
                if event_type == "assistant_delta":
                    delta = str(payload.get("delta") or "")
                    if hooks and hooks.on_assistant_delta:
                        maybe = hooks.on_assistant_delta(delta)
                        if inspect.isawaitable(maybe):
                            await maybe
                    continue
                if event_type == "reasoning_delta":
                    delta = str(payload.get("delta") or "")
                    if hooks and hooks.on_reasoning_delta:
                        maybe = hooks.on_reasoning_delta(delta)
                        if inspect.isawaitable(maybe):
                            await maybe
                    continue
                if event_type == "trace":
                    evt = payload.get("data")
                    if isinstance(evt, dict) and hooks and hooks.on_trace_event:
                        maybe = hooks.on_trace_event(evt)
                        if inspect.isawaitable(maybe):
                            await maybe
                    continue
                if event_type == "final":
                    if not payload.get("ok"):
                        raise RuntimeError(str(payload.get("error") or "automation ipc error"))
                    result_data = payload.get("result")
                    final_result = result_data if isinstance(result_data, dict) else {}
                    break
        finally:
            writer.close()
            await writer.wait_closed()

        data = final_result or {}
        usage = data.get("token_usage")
        if isinstance(usage, dict):
            self._token_usage_cache = usage
        try:
            self._turn_count_cache = int(data.get("turn_count", self._turn_count_cache))
        except Exception:
            pass
        return AgentRunResult(
            output_text=str(data.get("output_text") or ""),
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        )
