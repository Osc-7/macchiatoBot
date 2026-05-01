"""Local IPC bridge for long-running automation process."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from system.kernel.terminal import KernelTerminal

from agent_core.interfaces import (
    AgentHooks,
    AgentRunInput,
    AgentRunResult,
    InjectMessageCommand,
)
from agent_core.permissions.ask_user_registry import ask_user_ipc_stream_notify_scope
from agent_core.permissions.wait_registry import permission_ipc_stream_notify_scope

from .core_gateway import AutomationCoreGateway
# base64 图片可能数 MB，默认 64KB readline limit 远远不够
_STREAM_LIMIT = 32 * 1024 * 1024  # 32 MB
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
        terminal: Optional["KernelTerminal"] = None,
    ) -> None:
        self._gateway = gateway
        self._owner_id = owner_id.strip() or "root"
        self._source = source.strip() or "cli"
        self._socket_path = socket_path or default_socket_path()
        self._policy = policy or IPCServerPolicy()
        self._terminal = terminal
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
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(path), limit=_STREAM_LIMIT
        )
        # The daemon may run as root (for runuser-based bash isolation) while
        # frontends such as Feishu run as the deploy user, so the Unix socket
        # must be connectable across service users.
        path.chmod(0o666)
        self._expire_task = asyncio.create_task(
            self._expire_loop(), name="automation-ipc-expire"
        )

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
                await asyncio.wait_for(self._stopped.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            # scheduler 模式下，session 生命周期由 KernelScheduler._ttl_loop() 统一管理，
            # IPC 层不重复执行过期检查，避免两个循环同时 evict 同一 session 产生竞争。
            if self._gateway.has_scheduler:
                continue
            try:
                for sid in self._gateway.list_sessions():
                    if self._gateway.should_expire_session(session_id=sid):
                        await self._gateway.expire_session(
                            reason="timer", session_id=sid
                        )
            except Exception as exc:
                logger.warning("automation ipc expire loop failed: %s", exc)

    # 空闲连接超时：客户端建立连接后若超过此时长无数据，服务端关闭连接释放协程。
    _READ_IDLE_TIMEOUT: float = 1800.0  # 30 分钟

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(
                        reader.readline(), timeout=self._READ_IDLE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    break  # 空闲超时，关闭连接
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
                writer.write(
                    (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                )
                await writer.drain()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_run_turn_stream(
        self,
        req_id: Any,
        params: Dict[str, Any],
        writer: asyncio.StreamWriter,
    ) -> None:
        client_id = str(params.get("client_id") or "default")
        if client_id not in self._client_active_session:
            self._client_active_session[client_id] = f"{self._source}:{self._owner_id}"
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
        async def _forward_ask_user_stream(
            batch_id: str, payload: Dict[str, Any]
        ) -> None:
            await _send_event(
                "feishu_ask_user_notify",
                {"batch_id": batch_id, "payload": payload},
            )

        async def _forward_permission_stream(
            permission_id: str, payload: Dict[str, Any]
        ) -> None:
            await _send_event(
                "feishu_permission_notify",
                {"permission_id": permission_id, "payload": payload},
            )

        try:
            meta_dict: Dict[str, Any] = metadata if isinstance(metadata, dict) else {}
            _ci = meta_dict.get("content_items")
            if isinstance(_ci, list) and _ci:
                logger.info(
                    "ipc_server: run_turn_stream received %d content_items (types=%s)",
                    len(_ci),
                    [str(i.get("type")) for i in _ci[:3]],
                )
            async with ask_user_ipc_stream_notify_scope(_forward_ask_user_stream):
                async with permission_ipc_stream_notify_scope(_forward_permission_stream):
                    result = await self._gateway.inject_message(
                        InjectMessageCommand(
                            session_id=active_session,
                            input=AgentRunInput(text=text, metadata=meta_dict),
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
                        "attachments": getattr(result, "attachments", []),
                        "trace_events": trace_events,
                        "token_usage": usage,
                        "turn_count": turn_count,
                    },
                },
            )
        except (BrokenPipeError, ConnectionResetError) as exc:
            # 客户端在流式对话过程中主动断开连接（例如用户 Ctrl+C 或退出 CLI），
            # writer 已失效，继续写入只会产生噪音日志。此处记录一条调试信息后静默结束。
            logger.info(
                "automation ipc client disconnected during run_turn_stream "
                "(session_id=%s, client_id=%s, error=%s)",
                active_session,
                client_id,
                exc,
            )
        except Exception as exc:
            # 非连接类错误：尽量向仍然存活的客户端发送 final 错误事件；
            # 若此时连接也已断开，则忽略第二次 BrokenPipe/ConnectionReset。
            try:
                await _send_event("final", {"ok": False, "error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                logger.warning(
                    "failed to send error final event to disconnected client "
                    "(session_id=%s, client_id=%s, error=%s)",
                    active_session,
                    client_id,
                    exc,
                )

    async def _dispatch(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        client_id = str(params.get("client_id") or "default")
        if client_id not in self._client_active_session:
            self._client_active_session[client_id] = f"{self._source}:{self._owner_id}"
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
            return {
                "sessions": self._gateway.list_sessions(),
                "active_session_id": active_session,
            }

        if method == "session_switch":
            session_id = str(params.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("session_id 不能为空")
            create_if_missing = bool(params.get("create_if_missing", True))
            created = await self._gateway.ensure_session(
                session_id, create_if_missing=create_if_missing
            )
            self._client_active_session[client_id] = session_id
            self._gateway.mark_activity(session_id)
            return {"created": created, "active_session_id": session_id}

        if method == "session_delete":
            session_id = str(params.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("session_id 不能为空")
            # 任一客户端仍将此会话作为 active 时，不允许删除，避免并发使用中的状态错乱。
            if session_id in set(self._client_active_session.values()):
                return {
                    "deleted": False,
                    "active_session_id": self._client_active_session.get(client_id),
                }
            ok = await self._gateway.delete_session(session_id)
            # 如果客户端当前活跃会话被删除，则回退到默认会话标识；实际 CoreSession 需按需显式切换。
            if ok and self._client_active_session.get(client_id) == session_id:
                self._client_active_session[client_id] = f"{self._source}:{self._owner_id}"
            return {
                "deleted": ok,
                "active_session_id": self._client_active_session.get(client_id),
            }

        if method == "clear_context":
            await self._gateway.clear_context_for_session(active_session)
            return {"ok": True}

        if method == "compress_context":
            keep_raw = params.get("keep_recent_turns")
            try:
                keep = int(keep_raw) if keep_raw is not None else None
            except (TypeError, ValueError):
                keep = None
            result = await self._gateway.compress_context_for_session(
                active_session, keep_recent_turns=keep
            )
            return {"ok": True, "result": result}

        if method == "get_token_usage":
            usage = self._gateway.get_token_usage(session_id=active_session)
            return {"usage": usage}

        if method == "model_list":
            models = self._gateway.list_models(session_id=active_session)
            return {"models": models}

        if method == "model_switch":
            name = str(params.get("name") or "").strip()
            if not name:
                raise ValueError("name 不能为空")
            info = self._gateway.switch_model(name, session_id=active_session)
            return {"ok": True, "info": info}

        if method == "get_turn_count":
            turn_count = self._gateway.get_turn_count(session_id=active_session)
            return {"turn_count": turn_count}

        if method == "resolve_permission":
            # 卡片批准/拒绝由 feishu_ws_gateway 等独立进程触发，须在 daemon 内唤醒 Future
            from agent_core.permissions.wait_registry import (
                PermissionDecision,
                resolve_permission as _resolve_permission_wait,
            )

            pid = str(params.get("permission_id") or "").strip()
            if not pid:
                raise ValueError("permission_id 不能为空")
            allowed = bool(params.get("allowed"))
            clarify_requested = bool(params.get("clarify_requested"))
            path_prefix = params.get("path_prefix")
            note_raw = params.get("note")
            ui_merged: Optional[str] = None
            if clarify_requested:
                ui_merged = str(params.get("user_instruction") or "").strip()
            persist_acl = bool(params.get("persist_acl"))
            decision = PermissionDecision(
                allowed=allowed and not clarify_requested,
                path_prefix=str(path_prefix).strip() if path_prefix else None,
                note=None if note_raw is None else str(note_raw),
                clarify_requested=clarify_requested,
                user_instruction=ui_merged,
                persist_acl=persist_acl if allowed and not clarify_requested else False,
            )
            ok = _resolve_permission_wait(pid, decision)
            return {"ok": bool(ok)}

        if method == "resolve_ask_user":
            from agent_core.permissions.ask_user_registry import (
                parse_answers_from_ipc_params,
                resolve_ask_user as _resolve_ask_user_wait,
            )

            bid = str(params.get("batch_id") or params.get("ask_user_id") or "").strip()
            if not bid:
                raise ValueError("batch_id 不能为空")
            decision = parse_answers_from_ipc_params(params if isinstance(params, dict) else {})
            ok = _resolve_ask_user_wait(bid, decision)
            return {"ok": bool(ok)}

        if method == "submit_ask_user_fragment":
            from agent_core.permissions.ask_user_registry import (
                AskUserAnswer,
                submit_ask_user_fragment as _submit_au_fragment,
            )

            bid = str(params.get("batch_id") or "").strip()
            qid = str(params.get("question_id") or "").strip()
            if not bid or not qid:
                raise ValueError("batch_id / question_id 不能为空")
            so_raw = params.get("selected_option")
            ct_raw = params.get("custom_text")
            answer = AskUserAnswer(
                question_id=qid,
                selected_option=(
                    str(so_raw).strip()
                    if so_raw is not None and str(so_raw).strip()
                    else None
                ),
                custom_text=(
                    str(ct_raw).strip()
                    if ct_raw is not None and str(ct_raw).strip()
                    else None
                ),
            )
            ok, detail, snap = _submit_au_fragment(bid, answer)
            card = None
            if ok and snap:
                from frontend.feishu.ask_user_card import (
                    build_ask_user_card_from_registry_snapshot,
                )

                card = build_ask_user_card_from_registry_snapshot(snap, qid)
            return {"ok": bool(ok), "detail": detail, "card": card}

        if method == "poll_push":
            # 非阻塞轮询：批量取出该 session 所有 [out] 队列结果（统一出口）
            results = []
            if hasattr(self._gateway, "poll_push_result"):
                while True:
                    envelope = self._gateway.poll_push_result(active_session)
                    if envelope is None:
                        break
                    request_id, result = envelope
                    results.append(
                        {
                            "request_id": request_id,
                            "output_text": result.output_text,
                            "metadata": (
                                result.metadata
                                if isinstance(result.metadata, dict)
                                else {}
                            ),
                        }
                    )
            return {"results": results, "session_id": active_session}

        if method == "run_turn":
            text = str(params.get("text") or "")
            metadata = params.get("metadata")
            trace_events: list[dict[str, Any]] = []

            async def _on_trace_event(evt: Dict[str, Any]) -> None:
                trace_events.append(evt)

            hooks = AgentHooks(on_trace_event=_on_trace_event)
            meta_dict: Dict[str, Any] = metadata if isinstance(metadata, dict) else {}
            result = await self._gateway.inject_message(
                InjectMessageCommand(
                    session_id=active_session,
                    input=AgentRunInput(text=text, metadata=meta_dict),
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

        # ── KernelTerminal 系统控制台 RPC ─────────────────────────────────
        if method.startswith("terminal_"):
            if self._terminal is None:
                raise ValueError("KernelTerminal not available")
            t = self._terminal
        else:
            t = None
        if t is not None:
            if method == "terminal_ps":
                return {"cores": [asdict(c) for c in t.ps()]}

            if method == "terminal_top":
                return asdict(t.top())

            if method == "terminal_queue":
                return t.queue()

            if method == "terminal_automation_jobs":
                return t.automation_tracked_jobs()

            if method == "terminal_agent_tasks":
                lim = params.get("limit", 25)
                try:
                    limit_i = int(lim)
                except (TypeError, ValueError):
                    limit_i = 25
                return t.agent_task_queue_status(limit=limit_i)

            if method == "terminal_inspect":
                session_id = str(params.get("session_id") or "").strip()
                if not session_id:
                    raise ValueError("session_id 不能为空")
                return asdict(t.inspect(session_id))

            if method == "terminal_kill":
                session_id = str(params.get("session_id") or "").strip()
                if not session_id:
                    raise ValueError("session_id 不能为空")
                await t.kill(session_id)
                return {"ok": True}

            if method == "terminal_cancel":
                session_id = str(params.get("session_id") or "").strip()
                if not session_id:
                    raise ValueError("session_id 不能为空")
                cancelled = await t.cancel(session_id)
                return {"cancelled": cancelled}

            if method == "terminal_spawn":
                session_id = str(params.get("session_id") or "").strip()
                if not session_id:
                    raise ValueError("session_id 不能为空")
                source = str(params.get("source") or "system").strip() or "system"
                user_id = str(params.get("user_id") or "root").strip() or "root"
                core_info = await t.spawn(session_id, source=source, user_id=user_id)
                return asdict(core_info)

            if method == "terminal_attach":
                session_id = str(params.get("session_id") or "").strip()
                text = str(params.get("text") or "")
                if not session_id:
                    raise ValueError("session_id 不能为空")
                result = await t.attach(session_id, text)
                return {
                    "output_text": result.output_text,
                    "metadata": getattr(result, "metadata", {}),
                    "attachments": getattr(result, "attachments", []),
                }

            if method == "terminal_create_user":
                user_id = str(params.get("user_id") or "").strip()
                if not user_id:
                    raise ValueError("user_id 不能为空")
                frontend = str(params.get("frontend") or "cli").strip() or "cli"
                warm_spawn = bool(params.get("warm_spawn", False))
                return await t.create_logic_user(
                    user_id, frontend=frontend, warm_spawn=warm_spawn
                )

            if method == "terminal_list_users":
                frontend = str(params.get("frontend") or "cli").strip() or "cli"
                return t.list_logic_users(frontend=frontend)

            raise ValueError(f"unknown terminal method: {method}")

        raise ValueError(f"unknown method: {method}")


class AutomationIPCClient:
    """Async client for AutomationIPCServer."""

    def __init__(
        self,
        *,
        owner_id: str = "root",
        source: str = "cli",
        socket_path: Optional[str] = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.owner_id = owner_id.strip() or "root"
        self.source = source.strip() or "cli"
        self.active_session_id = f"{self.source}:{self.owner_id}"
        self._socket_path = socket_path or default_socket_path()
        self._timeout_seconds = float(timeout_seconds)
        self._token_usage_cache: Dict[str, Any] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "call_count": 0,
            "cost_yuan": 0.0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
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
        self.active_session_id = str(
            data.get("active_session_id") or self.active_session_id
        )

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
            asyncio.open_unix_connection(self._socket_path, limit=_STREAM_LIMIT),
            timeout=self._timeout_seconds,
        )
        req = {
            "id": f"{self._client_id}:{method}",
            "method": method,
            "params": {"client_id": self._client_id, **params},
        }
        try:
            writer.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
            await asyncio.wait_for(writer.drain(), timeout=self._timeout_seconds)
            raw = await asyncio.wait_for(
                reader.readline(), timeout=self._timeout_seconds
            )
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
        self.active_session_id = str(
            data.get("active_session_id") or self.active_session_id
        )
        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            return []
        return [str(s) for s in sessions]

    async def switch_session(
        self, session_id: str, *, create_if_missing: bool = True
    ) -> bool:
        data = await self._request(
            "session_switch",
            {"session_id": session_id, "create_if_missing": create_if_missing},
        )
        self.active_session_id = str(data.get("active_session_id") or session_id)
        return bool(data.get("created", False))

    async def delete_session(self, session_id: str) -> bool:
        data = await self._request(
            "session_delete",
            {"session_id": session_id},
        )
        # 如果服务器端将 active_session_id 回退，这里也同步一下。
        maybe_active = data.get("active_session_id")
        if isinstance(maybe_active, str) and maybe_active:
            self.active_session_id = maybe_active
        return bool(data.get("deleted", False))

    async def clear_context(self) -> None:
        await self._request("clear_context", {})

    async def compress_context(
        self,
        keep_recent_turns: Optional[int] = None,
    ) -> Dict[str, Any]:
        """请求 daemon 侧对当前 session 的上下文做主动压缩。

        返回结构化结果（``compressed`` / ``messages_before`` / ``messages_after``
        / ``summary_chars`` / ``current_tokens`` / ``threshold_tokens`` / ``model``
        / ``compression_round`` / ``session_loaded`` 等），由前端格式化为人类可读消息。
        """
        params: Dict[str, Any] = {}
        if isinstance(keep_recent_turns, int) and keep_recent_turns > 0:
            params["keep_recent_turns"] = keep_recent_turns
        data = await self._request("compress_context", params)
        result = data.get("result")
        return dict(result) if isinstance(result, dict) else {}

    async def list_models(self) -> List[Dict[str, Any]]:
        """列出 daemon 侧当前 session 可用的 LLM provider。"""
        data = await self._request("model_list", {})
        models = data.get("models")
        if not isinstance(models, list):
            return []
        return [dict(m) for m in models if isinstance(m, dict)]

    async def switch_model(self, name: str) -> Dict[str, Any]:
        """请求 daemon 侧切换当前 session 的 LLM provider。"""
        data = await self._request("model_switch", {"name": name})
        info = data.get("info")
        return dict(info) if isinstance(info, dict) else {}

    async def get_token_usage(self) -> dict:
        data = await self._request("get_token_usage", {})
        usage = data.get("usage")
        if isinstance(usage, dict):
            self._token_usage_cache = {**self._token_usage_cache, **usage}
        return dict(self._token_usage_cache)

    async def get_turn_count(self) -> int:
        data = await self._request("get_turn_count", {})
        try:
            self._turn_count_cache = int(data.get("turn_count", 0))
        except Exception:
            self._turn_count_cache = 0
        return self._turn_count_cache

    async def resolve_permission(
        self,
        *,
        permission_id: str,
        allowed: bool,
        path_prefix: Optional[str] = None,
        note: Optional[str] = None,
        clarify_requested: bool = False,
        user_instruction: Optional[str] = None,
        persist_acl: bool = False,
    ) -> bool:
        """在 daemon 进程内调用 resolve_permission（跨进程网关须用此接口）。

        persist_acl 应仅由飞书卡片等人类交互回填；勿由自动化逻辑伪造。"""
        payload: Dict[str, Any] = {
            "permission_id": permission_id,
            "allowed": allowed,
            "path_prefix": path_prefix,
            "note": note,
            "clarify_requested": clarify_requested,
            "persist_acl": persist_acl,
        }
        if user_instruction is not None:
            payload["user_instruction"] = user_instruction
        data = await self._request("resolve_permission", payload)
        return bool(data.get("ok"))

    async def resolve_ask_user(
        self,
        *,
        batch_id: str,
        answers: List[Dict[str, Any]],
    ) -> bool:
        """在 daemon 进程内调用 resolve_ask_user（跨进程网关须用此接口）。

        answers 每项建议包含 question_id、selected_option 与/或 custom_text。
        """
        payload: Dict[str, Any] = {
            "batch_id": batch_id,
            "answers": answers,
        }
        data = await self._request("resolve_ask_user", payload)
        return bool(data.get("ok"))

    async def submit_ask_user_fragment(
        self,
        *,
        batch_id: str,
        question_id: str,
        selected_option: Optional[str] = None,
        custom_text: Optional[str] = None,
    ) -> tuple[bool, str, Optional[Dict[str, Any]]]:
        """飞书分题提交：在 daemon 内合并 partial，集齐后唤醒 ask_user Future。

        第三项为刷新后的卡片 JSON（与 request_permission 回调更新卡片一致）；失败或无刷新时为 None。
        """
        payload: Dict[str, Any] = {
            "batch_id": batch_id,
            "question_id": question_id,
        }
        if selected_option is not None:
            payload["selected_option"] = selected_option
        if custom_text is not None:
            payload["custom_text"] = custom_text
        data = await self._request("submit_ask_user_fragment", payload)
        card = data.get("card")
        return (
            bool(data.get("ok")),
            str(data.get("detail") or ""),
            card if isinstance(card, dict) else None,
        )

    async def poll_push(self) -> list:
        """非阻塞轮询当前 session 的 inject_turn 推送结果列表（空则返回 []）。

        供 CLI 后台通知循环调用：有新的 subagent 完成通知时，主 agent 会在后台处理
        并通过 inject_turn 产生回复，该回复通过此接口推送给前端展示。
        """
        data = await self._request("poll_push", {})
        results = data.get("results")
        return results if isinstance(results, list) else []

    async def run_turn(
        self, agent_input: AgentRunInput, hooks: AgentHooks | None = None
    ) -> AgentRunResult:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self._socket_path, limit=_STREAM_LIMIT),
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
        await asyncio.wait_for(writer.drain(), timeout=self._timeout_seconds)

        final_result: Optional[Dict[str, Any]] = None
        try:
            while True:
                raw = await asyncio.wait_for(
                    reader.readline(), timeout=self._timeout_seconds
                )
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
                if event_type == "feishu_ask_user_notify":
                    bid = str(payload.get("batch_id") or "").strip()
                    pl = payload.get("payload")
                    fn = getattr(hooks, "on_feishu_ask_user_notify", None) if hooks else None
                    if fn and bid and isinstance(pl, dict):
                        maybe = fn(bid, pl)
                        if inspect.isawaitable(maybe):
                            await maybe
                    continue
                if event_type == "feishu_permission_notify":
                    pid = str(payload.get("permission_id") or "").strip()
                    pl = payload.get("payload")
                    fn = (
                        getattr(hooks, "on_feishu_permission_notify", None)
                        if hooks
                        else None
                    )
                    if fn and pid and isinstance(pl, dict):
                        maybe = fn(pid, pl)
                        if inspect.isawaitable(maybe):
                            await maybe
                    continue
                if event_type == "final":
                    if not payload.get("ok"):
                        raise RuntimeError(
                            str(payload.get("error") or "automation ipc error")
                        )
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
        meta = data.get("metadata")
        meta_dict: Dict[str, Any] = meta if isinstance(meta, dict) else {}
        attachments = data.get("attachments")
        if not isinstance(attachments, list):
            attachments = []
        output_text = str(data.get("output_text") or "")
        # 对端提前断开或网络中断时可能收不到 final 事件，避免飞书等前端完全无反馈
        if final_result is None and not output_text.strip():
            output_text = (
                "连接已中断，未收到完整回复，请稍后重试。"
            )
            meta_dict = {**meta_dict, "_ipc_error": "stream_incomplete"}
        return AgentRunResult(
            output_text=output_text,
            metadata=meta_dict,
            attachments=attachments,
        )

    # ── KernelTerminal 系统控制台（需 daemon 已注入 terminal）───────────────

    async def terminal_ps(self) -> list:
        """列出所有活跃 Core。返回 [CoreInfo 的 dict, ...]。"""
        data = await self._request("terminal_ps", {})
        cores = data.get("cores")
        return cores if isinstance(cores, list) else []

    async def terminal_top(self) -> Dict[str, Any]:
        """系统概览：active_cores, max_cores, queue_depth, inflight_tasks, uptime_seconds。"""
        data = await self._request("terminal_top", {})
        return data if isinstance(data, dict) else {}

    async def terminal_queue(self) -> Dict[str, Any]:
        """队列状态：queue_size, inflight_sessions, cancelled_sessions, active_task_count。"""
        data = await self._request("terminal_queue", {})
        return data if isinstance(data, dict) else {}

    async def terminal_automation_jobs(self) -> Dict[str, Any]:
        """AutomationScheduler 追踪的每个 job 的协程状态与定义摘要。"""
        data = await self._request("terminal_automation_jobs", {})
        return data if isinstance(data, dict) else {}

    async def terminal_agent_tasks(self, *, limit: int = 25) -> Dict[str, Any]:
        """AgentTask 持久化队列：pending/running 计数与最近任务列表。"""
        data = await self._request("terminal_agent_tasks", {"limit": int(limit)})
        return data if isinstance(data, dict) else {}

    async def terminal_inspect(self, session_id: str) -> Dict[str, Any]:
        """查看指定 session 的详细信息。"""
        return await self._request("terminal_inspect", {"session_id": session_id})

    async def terminal_kill(self, session_id: str) -> None:
        """终结指定 Core。"""
        await self._request("terminal_kill", {"session_id": session_id})

    async def terminal_cancel(self, session_id: str) -> bool:
        """取消该 session 正在运行的任务，不销毁 Core。返回是否取消了任务。"""
        data = await self._request("terminal_cancel", {"session_id": session_id})
        return bool(data.get("cancelled", False))

    async def terminal_spawn(
        self,
        session_id: str,
        *,
        source: str = "system",
        user_id: str = "root",
    ) -> Dict[str, Any]:
        """创建新 Core。返回 CoreInfo 的 dict。"""
        return await self._request(
            "terminal_spawn",
            {"session_id": session_id, "source": source, "user_id": user_id},
        )

    async def terminal_attach(self, session_id: str, text: str) -> AgentRunResult:
        """以系统身份向指定 session 发一条消息，等待 Agent 回复。"""
        data = await self._request(
            "terminal_attach",
            {"session_id": session_id, "text": text},
        )
        return AgentRunResult(
            output_text=str(data.get("output_text") or ""),
            metadata=data.get("metadata") or {},
            attachments=data.get("attachments") or [],
        )

    async def terminal_create_user(
        self,
        user_id: str,
        *,
        frontend: str = "cli",
        warm_spawn: bool = False,
    ) -> Dict[str, Any]:
        """在记忆库下创建逻辑用户目录布局；可选预热 Core 会话。"""
        data = await self._request(
            "terminal_create_user",
            {
                "user_id": user_id,
                "frontend": frontend,
                "warm_spawn": bool(warm_spawn),
            },
        )
        return data if isinstance(data, dict) else {}

    async def terminal_list_users(self, *, frontend: str = "cli") -> Dict[str, Any]:
        """列出某 frontend 下已有记忆目录的用户 id。"""
        data = await self._request("terminal_list_users", {"frontend": frontend})
        return data if isinstance(data, dict) else {}
