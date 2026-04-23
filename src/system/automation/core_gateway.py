"""In-process Automation gateway for channel -> core dispatch."""

from __future__ import annotations

import inspect
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional

from .session_registry import SessionRegistry
from agent_core.interfaces import (
    AgentHooks,
    AgentRunInput,
    AgentRunResult,
    CoreSession,
    InjectMessageCommand,
)

if TYPE_CHECKING:
    from system.kernel import KernelScheduler

logger = logging.getLogger(__name__)

_PUSH_BUFFER_MAXLEN = 50


def _enrich_content_refs_with_context(
    raw_refs: List[Any],
    *,
    source: str,
    user_id: str,
) -> List[Dict[str, Any]]:
    """在前端传入的 content_refs 上补齐 source/user_id，供 resolver 落盘到正确工作区。"""
    enriched: List[Dict[str, Any]] = []
    for raw in raw_refs or []:
        if isinstance(raw, dict):
            item = dict(raw)
        else:
            # 兼容已经是 ContentReference 的情况
            try:
                item = raw.to_dict()  # type: ignore[attr-defined]
            except Exception:
                continue
        extra = item.get("extra")
        extra_dict = dict(extra) if isinstance(extra, dict) else {}
        extra_dict.setdefault("source", source)
        extra_dict.setdefault("user_id", user_id)
        item["extra"] = extra_dict
        enriched.append(item)
    return enriched


CoreSessionFactory = Callable[[str], CoreSession | Awaitable[CoreSession]]


@dataclass
class SessionCutPolicy:
    idle_timeout_minutes: int = 30
    daily_cutoff_hour: int = 4


class AutomationCoreGateway:
    """
    进程内 Automation 网关。

    将 CLI / 其他 channel 的输入转成 KernelRequest，通过 KernelScheduler 下发。
    请求投入 InputQueue，由 Scheduler 异步分发，支持跨 session 真并发和 OutputBus 结果等待。

    IPC 协议（AutomationIPCServer）和外部接口完全不变。
    """

    def __init__(
        self,
        core_session: CoreSession,
        *,
        kernel_scheduler: "KernelScheduler",
        session_id: str = "cli:root",
        policy: Optional[SessionCutPolicy] = None,
        session_factory: Optional[CoreSessionFactory] = None,
        owner_id: str = "root",
        source: str = "cli",
        session_registry: Optional[SessionRegistry] = None,
    ):
        self._kernel_scheduler: "KernelScheduler" = kernel_scheduler
        self._sessions: Dict[str, CoreSession] = {session_id: core_session}
        self._owned_sessions: set[str] = set()
        self._active_session_id = session_id
        self._owner_id = owner_id.strip() or "root"
        self._source = source.strip() or "cli"
        self._policy = policy or SessionCutPolicy()
        now = datetime.now()
        self._last_activity: Dict[str, datetime] = {session_id: now}
        self._session_factory = session_factory
        self._session_registry = session_registry or SessionRegistry()
        # 在 upsert_session（会重置 is_expired=0）之前先记录过期状态，供 activate_primary_session 使用
        self._initial_session_was_expired: bool = self._session_registry.is_expired(
            self._owner_id, self._source, session_id
        )
        self._session_registry.upsert_session(self._owner_id, self._source, session_id)
        # 本地输出缓冲：通过 OutputBus subscriber 接收非 submit 的结果（inject_turn 等）
        self._pending_submits: set[str] = set()
        self._push_buffers: Dict[str, deque] = {}
        self._subscriptions: Dict[str, str] = {}  # session_id -> subscription_id

    @property
    def config(self):
        # 兼容 interactive.py 现有读取方式；active 在 pool 时从 entry 取 config
        entry = self._kernel_scheduler.core_pool.get_entry(self._active_session_id)
        if entry is not None:
            return getattr(entry.agent, "config", None)
        session = self._sessions.get(self._active_session_id)
        if session is not None:
            return getattr(session, "config", None)
        fallback = next(iter(self._sessions.values()), None)
        return getattr(fallback, "config", None) if fallback else None

    @property
    def raw_core_session(self) -> CoreSession:
        session = self._sessions.get(self._active_session_id)
        if session is not None:
            return session
        entry = self._kernel_scheduler.core_pool.get_entry(self._active_session_id)
        if entry is not None:
            return entry.agent  # AgentCore 满足 config 等基础接口
        fallback = next(iter(self._sessions.values()), None)
        if fallback is not None:
            return fallback
        raise RuntimeError(f"active session not found: {self._active_session_id}")

    @property
    def active_session_id(self) -> str:
        return self._active_session_id

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def source(self) -> str:
        return self._source

    async def activate_primary_session(self) -> None:
        """
        激活主会话，根据创建时记录的 is_expired 状态决定是否重放历史消息。

        用于取代调用方直接调用 core_session.activate_session(session_id)，
        确保过期会话以空上下文启动，而非全量重放历史。
        """
        session = self._sessions.get(self._active_session_id)
        if session is None:
            return
        activate = getattr(session, "activate_session", None)
        if not callable(activate):
            return
        replay_limit: Optional[int] = 0 if self._initial_session_was_expired else None
        logger.info(
            "activate_primary_session: session_id=%s, was_expired=%s, replay_limit=%s",
            self._active_session_id,
            self._initial_session_was_expired,
            replay_limit,
        )
        maybe = activate(self._active_session_id, replay_messages_limit=replay_limit)
        if inspect.isawaitable(maybe):
            await maybe

    def list_sessions(self) -> list[str]:
        seen = set(self._sessions.keys())
        try:
            seen.update(self._kernel_scheduler.core_pool.list_sessions())
        except Exception:
            pass
        for sid in self._session_registry.list_sessions(self._owner_id, self._source):
            seen.add(sid)
        return sorted(seen)

    async def ensure_session(
        self, session_id: str, *, create_if_missing: bool = True
    ) -> bool:
        """
        确保某个 session 已可用，但不改变当前 active_session_id。

        Returns:
            是否为新创建的 session
        """
        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session_id 不能为空")
        existed_any = (
            session_id in self._sessions
            or self._session_registry.session_exists(
                self._owner_id, self._source, session_id
            )
        )
        try:
            existed_any = (
                existed_any
                or self._kernel_scheduler.core_pool.has_session(session_id)
            )
        except Exception:
            pass
        if session_id not in self._sessions:
            if not create_if_missing and not existed_any:
                raise KeyError(f"session not found: {session_id}")
            self._session_registry.upsert_session(
                self._owner_id, self._source, session_id
            )
            self._last_activity[session_id] = datetime.now()
        return not existed_any

    async def switch_session(
        self, session_id: str, *, create_if_missing: bool = True
    ) -> bool:
        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session_id 不能为空")
        existed_any = (
            session_id in self._sessions
            or self._session_registry.session_exists(
                self._owner_id, self._source, session_id
            )
        )
        try:
            existed_any = (
                existed_any
                or self._kernel_scheduler.core_pool.has_session(session_id)
            )
        except Exception:
            pass
        created = False
        if session_id not in self._sessions:
            if not create_if_missing and not existed_any:
                raise KeyError(f"session not found: {session_id}")
            if not existed_any:
                self._session_registry.upsert_session(
                    self._owner_id, self._source, session_id
                )
                created = True
            self._last_activity[session_id] = datetime.now()
        self._active_session_id = session_id
        self.mark_activity(session_id)
        return created

    @property
    def has_scheduler(self) -> bool:
        """始终为 True；session 生命周期由 KernelScheduler._ttl_loop() 统一管理。"""
        return True

    def _ensure_subscribed(self, session_id: str) -> None:
        """确保已订阅指定 session 的 OutputBus 广播。"""
        if session_id in self._subscriptions:
            return
        sub_id = self._kernel_scheduler.subscribe_out(
            session_id,
            lambda req_id, result, _sid=session_id: self._on_session_output(
                _sid, req_id, result
            ),
        )
        self._subscriptions[session_id] = sub_id

    def _on_session_output(
        self, session_id: str, request_id: str, result: AgentRunResult
    ) -> None:
        """OutputBus listener 回调：跳过 submit 路径结果，缓冲其余结果供 poll_push_result 消费。"""
        if request_id in self._pending_submits:
            return
        if session_id not in self._push_buffers:
            self._push_buffers[session_id] = deque(maxlen=_PUSH_BUFFER_MAXLEN)
        self._push_buffers[session_id].append((request_id, result))
        logger.debug(
            "gateway: buffered output session_id=%s request_id=%s buf_size=%d",
            session_id,
            request_id[:8] if request_id else "",
            len(self._push_buffers[session_id]),
        )

    def poll_push_result(
        self, session_id: str
    ) -> Optional[tuple[str, AgentRunResult]]:
        """非阻塞：弹出该 session 的下一条推送结果，无则返回 None。

        结果来源于 OutputBus subscriber 回调缓冲的 inject_turn 等非 submit 结果。
        返回 (request_id, result)。典型使用方：IPC poll_push、CLI/Feishu 后台轮询。
        """
        buf = self._push_buffers.get(session_id)
        if not buf:
            return None
        return buf.popleft()

    async def run_turn(
        self,
        agent_input: AgentRunInput,
        hooks: AgentHooks | None = None,
    ) -> AgentRunResult:
        return await self._run_turn_via_scheduler(
            self._active_session_id, agent_input, hooks
        )

    async def _run_turn_via_scheduler(
        self,
        session_id: str,
        agent_input: AgentRunInput,
        hooks: AgentHooks | None = None,
    ) -> AgentRunResult:
        """
        通过 KernelScheduler 提交请求并等待结果。

        submit() 仅提交到 [in] 队列并返回 request_id；
        再通过 wait_result(request_id) 在 out 总线上等待结果。

        注意：Scheduler 只消费 metadata["content_items"]，不解析 content_refs。
        飞书等前端传 content_refs，此处需先解析为 content_items 再交给 Scheduler，
        确保当轮首条 LLM 请求即包含图片等多模态内容，而非让 AI 调用 attach_media。
        """
        from agent_core.content import ContentReference, resolve_content_refs
        from agent_core.kernel_interface import KernelRequest

        metadata = dict(agent_input.metadata)
        metadata.setdefault("source", self._source)
        metadata.setdefault("user_id", self._owner_id)
        if hooks is not None:
            metadata["_hooks"] = hooks

        # 前端可能已 pre-resolved content_items（避免 daemon 缺少对应 resolver）
        pre_items = metadata.get("content_items")
        if isinstance(pre_items, list) and pre_items:
            logger.info(
                "gateway: received pre-resolved content_items count=%d types=%s",
                len(pre_items),
                [str(i.get("type")) for i in pre_items[:3]],
            )

        # 将 content_refs（飞书 image_key 等）解析为 content_items，供 Scheduler 注入首轮 LLM
        raw_refs = metadata.get("content_refs")
        if isinstance(raw_refs, list) and raw_refs:
            try:
                refs = [
                    ContentReference.from_dict(r)
                    for r in _enrich_content_refs_with_context(
                        raw_refs,
                        source=str(metadata.get("source") or self._source or "feishu"),
                        user_id=str(metadata.get("user_id") or self._owner_id or "unknown"),
                    )
                ]
                content_items = await resolve_content_refs(refs)
                if content_items:
                    metadata["content_items"] = content_items
            except Exception as exc:
                logger.warning("content_refs resolve failed before scheduler: %s", exc)

        profile = metadata.pop("_core_profile", None)
        frontend_id = self._source
        if profile is None and (session_id or "").startswith("shuiyuan:"):
            username = session_id.split(":", 1)[1] if ":" in session_id else "default"
            from agent_core.kernel_interface import CoreProfile
            from agent_core.config import get_config

            profile = CoreProfile.for_shuiyuan(
                dialog_window_id=username,
                tools_config=get_config().tools,
            )
            frontend_id = "shuiyuan"
            metadata.setdefault("user_id", username)
        request = KernelRequest.create(
            text=agent_input.text,
            session_id=session_id,
            frontend_id=frontend_id,
            metadata=metadata,
            profile=profile,
        )
        # 确保订阅该 session 的输出（用于接收后续 inject_turn 结果）
        self._ensure_subscribed(session_id)
        # 标记为 pending submit，listener 回调会跳过此 request_id 的结果
        self._pending_submits.add(request.request_id)
        try:
            submit_handle = await self._kernel_scheduler.submit(request)
            result: AgentRunResult = await self._kernel_scheduler.wait_result(submit_handle)
        finally:
            self._pending_submits.discard(request.request_id)
        self.mark_activity(session_id)
        return result

    async def inject_message(
        self,
        command: InjectMessageCommand,
        hooks: AgentHooks | None = None,
    ) -> AgentRunResult:
        result = await self._run_turn_via_scheduler(
            command.session_id,
            command.input,
            hooks=hooks,
        )
        self.mark_activity(command.session_id)
        return result

    def mark_activity(self, session_id: Optional[str] = None) -> None:
        sid = session_id or self._active_session_id
        now = datetime.now()
        self._last_activity[sid] = now
        self._session_registry.upsert_session(self._owner_id, self._source, sid)

    def should_expire_session(self, session_id: Optional[str] = None) -> bool:
        sid = session_id or self._active_session_id
        if self._session_registry.is_expired(self._owner_id, self._source, sid):
            return False
        now = datetime.now()
        last_activity = self._last_activity.get(sid)
        if last_activity is None:
            registry_ts = self._session_registry.get_updated_at(
                self._owner_id, self._source, sid
            )
            last_activity = registry_ts or now
            self._last_activity[sid] = last_activity
        idle_seconds = (now - last_activity).total_seconds()
        if idle_seconds >= self._policy.idle_timeout_minutes * 60:
            return True
        if (
            last_activity.date() < now.date()
            and now.hour >= self._policy.daily_cutoff_hour
        ):
            return True
        if (
            last_activity.date() == now.date()
            and last_activity.hour < self._policy.daily_cutoff_hour <= now.hour
        ):
            return True
        return False

    async def expire_session(
        self, reason: str = "session_expire", *, session_id: Optional[str] = None
    ) -> None:
        sid = session_id or self._active_session_id
        try:
            await self._kernel_scheduler.core_pool.evict(sid)
        except Exception as exc:
            logger.warning(
                "evict session failed (session_id=%s, reason=%s): %s",
                sid,
                reason,
                exc,
            )
        self._session_registry.mark_expired(self._owner_id, self._source, sid)
        self._last_activity[sid] = datetime.now()

    async def expire_session_if_needed(self, reason: str = "session_expire") -> bool:
        sid = self._active_session_id
        if not self.should_expire_session(sid):
            return False
        await self.expire_session(reason=reason, session_id=sid)
        return True

    async def finalize_session(self):
        entry = self._kernel_scheduler.core_pool.get_entry(self._active_session_id)
        if entry is not None:
            fn = getattr(entry.agent, "finalize_session", None)
            if callable(fn):
                maybe = fn()
                return await maybe if inspect.isawaitable(maybe) else maybe
        return None

    def reset_session(self) -> None:
        entry = self._kernel_scheduler.core_pool.get_entry(self._active_session_id)
        if entry is not None:
            fn = getattr(entry.agent, "reset_session", None)
            if callable(fn):
                fn()
        self.mark_activity(self._active_session_id)

    async def clear_context_for_session(self, session_id: str) -> None:
        entry = self._kernel_scheduler.core_pool.get_entry(session_id)
        if entry is not None:
            clear_fn = getattr(entry.agent, "clear_context", None)
            if callable(clear_fn):
                clear_fn()

    async def compress_context_for_session(
        self,
        session_id: str,
        *,
        keep_recent_turns: Optional[int] = None,
    ) -> Dict[str, Any]:
        """主动触发指定 session 的 AgentCore 压缩，返回结构化结果给 IPC 客户端。

        若 session 当前未驻留（CorePool 没有 live entry），返回未命中的占位结果。
        """
        entry = self._kernel_scheduler.core_pool.get_entry(session_id)
        if entry is None:
            return {
                "compressed": False,
                "summary": "",
                "summary_chars": 0,
                "messages_before": 0,
                "messages_after": 0,
                "kept": 0,
                "current_tokens": 0,
                "threshold_tokens": 0,
                "compression_round": 0,
                "model": "",
                "session_loaded": False,
            }
        fn = getattr(entry.agent, "compress_context", None)
        if not callable(fn):
            return {
                "compressed": False,
                "summary": "",
                "summary_chars": 0,
                "messages_before": 0,
                "messages_after": 0,
                "kept": 0,
                "current_tokens": 0,
                "threshold_tokens": 0,
                "compression_round": 0,
                "model": "",
                "session_loaded": True,
                "supported": False,
            }
        result = await fn(keep_recent_turns=keep_recent_turns)
        if isinstance(result, dict):
            result.setdefault("session_loaded", True)
            result.setdefault("supported", True)
            return result
        return {"compressed": False, "session_loaded": True, "supported": True}

    def clear_context(self) -> None:
        entry = self._kernel_scheduler.core_pool.get_entry(self._active_session_id)
        if entry is not None:
            clear_fn = getattr(entry.agent, "clear_context", None)
            if callable(clear_fn):
                clear_fn()

    _DEFAULT_USAGE = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "call_count": 0,
        "cost_yuan": 0.0,
    }

    def get_token_usage(self, session_id: Optional[str] = None) -> dict:
        sid = session_id or self._active_session_id
        entry = self._kernel_scheduler.core_pool.get_entry(sid)
        if entry is not None:
            fn = getattr(entry.agent, "get_token_usage", None)
            if callable(fn):
                result = fn()
                if isinstance(result, dict):
                    return {**self._DEFAULT_USAGE, **result}
        return dict(self._DEFAULT_USAGE)

    @staticmethod
    def _resolve_vision_provider_name_static(
        llm: Any, prov_map: Dict[str, Any]
    ) -> Optional[str]:
        vision_raw = getattr(llm, "vision_provider", None)
        vision_name: Optional[str] = (
            str(vision_raw) if vision_raw and str(vision_raw) in prov_map else None
        )
        if not vision_name:
            for n, ent in prov_map.items():
                caps_m = getattr(ent, "capabilities", None)
                if caps_m is not None and bool(getattr(caps_m, "vision", False)):
                    vision_name = str(n)
                    break
        return vision_name

    @staticmethod
    def _normalize_provider_key(key: str) -> str:
        """将 provider 名对齐到 ``config.llm.providers`` 的实际键（区分大小写不一致）。"""
        from agent_core.config import get_config

        raw = (key or "").strip()
        if not raw:
            return ""
        prov = getattr(get_config().llm, "providers", None) or {}
        if raw in prov:
            return raw
        rl = raw.lower()
        for k in prov:
            if k.lower() == rl:
                return k
        return raw

    def _canonical_active_provider_key(self, session_id: str) -> str:
        """
        当前会话「主对话」对应的 providers 键：预选 > live Agent 的 LLMClient > 全局 llm.active > 首个 provider。
        飞书与 CLI 共用；与仅依赖 AgentCore.list_models 内比较相比，可避免键名不一致导致全无 *。
        """
        from agent_core.config import get_config

        sid = (session_id or "").strip()
        pool = self._kernel_scheduler.core_pool
        if sid:
            pref = pool.get_session_preferred_llm_provider(sid)
            if pref:
                return self._normalize_provider_key(str(pref))
            live = pool.get_live_entry(sid)
            if live is not None and getattr(live, "agent", None) is not None:
                llm_c = getattr(live.agent, "_llm_client", None)
                if llm_c is not None:
                    an = getattr(llm_c, "active_provider_name", None)
                    # 仅采纳真实 str；单测里 CorePool 为 MagicMock 时 getattr 会得到 MagicMock，
                    # str(MagicMock) 不是合法 provider 名，会误伤 is_active。
                    if isinstance(an, str) and an.strip():
                        cand = self._normalize_provider_key(an)
                        prov_check = getattr(get_config().llm, "providers", None) or {}
                        if cand in prov_check or any(
                            k.lower() == cand.lower() for k in prov_check
                        ):
                            return cand
        llm = get_config().llm
        prov_map = getattr(llm, "providers", None) or {}
        if not prov_map:
            return ""
        a = getattr(llm, "active", None)
        if a:
            na = self._normalize_provider_key(str(a))
            if na in prov_map:
                return na
        return next(iter(prov_map.keys()))

    def _apply_model_list_active_flags(
        self, models: List[Dict[str, Any]], session_id: str
    ) -> None:
        """统一写入 is_active / is_vision_provider，避免各路径比较不一致。"""
        if not models:
            return
        from agent_core.config import get_config

        sid = (session_id or "").strip()
        active_key = self._canonical_active_provider_key(sid)
        llm = get_config().llm
        prov_map = getattr(llm, "providers", None) or {}
        vision_name = self._resolve_vision_provider_name_static(llm, prov_map)
        vision_key = (
            self._normalize_provider_key(str(vision_name))
            if vision_name
            else ""
        )

        for m in models:
            if not isinstance(m, dict):
                continue
            row_key = self._normalize_provider_key(str(m.get("name") or ""))
            m["is_active"] = bool(active_key) and row_key == active_key
            m["is_vision_provider"] = bool(vision_key) and row_key == vision_key

    def _list_models_from_global_llm_config(
        self, session_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        尚无 live Core 时从配置构造列表（与 AgentCore.list_models 字段对齐）。
        ``CorePool`` 中若已有该 session 的预选主模型，则 ``is_active`` 以预选为准。
        """
        from agent_core.config import CapabilitiesModel, get_config

        cfg = get_config()
        llm = cfg.llm
        prov_map = getattr(llm, "providers", None) or {}
        if not prov_map:
            return []
        vision_name = self._resolve_vision_provider_name_static(llm, prov_map)

        out: List[Dict[str, Any]] = []
        for name, ent in prov_map.items():
            caps_m = getattr(ent, "capabilities", None)
            # 缺 capabilities 时仍要列出该行；否则当前 active 若恰被 skip，列表里会没有任何 *。
            if caps_m is None:
                caps_m = CapabilitiesModel()
            api_model = str(getattr(ent, "model", "") or "")
            out.append(
                {
                    "name": name,
                    "model": api_model,
                    "api_model": api_model,
                    "label": getattr(ent, "label", None),
                    "base_url": getattr(ent, "base_url", None),
                    "vision": bool(getattr(caps_m, "vision", False)),
                    "function_calling": bool(getattr(caps_m, "function_calling", True)),
                    "reasoning_content": bool(
                        getattr(caps_m, "reasoning_content", False)
                    ),
                    "context_window": getattr(caps_m, "context_window", None),
                    "is_active": False,
                    "is_vision_provider": vision_name is not None and name == vision_name,
                }
            )
        sid = (session_id or self._active_session_id or "").strip()
        self._apply_model_list_active_flags(out, sid)
        return out

    def list_models(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出 session 所在 agent 的可用 LLM provider。"""
        sid = session_id or self._active_session_id
        sid = (sid or "").strip()
        live = self._kernel_scheduler.core_pool.get_live_entry(sid)
        if live is not None and getattr(live, "agent", None) is not None:
            fn = getattr(live.agent, "list_models", None)
            if callable(fn):
                try:
                    result = fn()
                    if isinstance(result, list) and result:
                        out = list(result)
                        self._apply_model_list_active_flags(out, sid)
                        return out
                except Exception:
                    logger.debug(
                        "agent.list_models failed for session_id=%s", sid, exc_info=True
                    )
        return self._list_models_from_global_llm_config(session_id=sid)

    def _switch_result_from_config_provider(self, query: str) -> Dict[str, Any]:
        """无 live AgentCore 时根据配置构造与 ``AgentCore.switch_model`` 一致的结果字段。"""
        from agent_core.config import get_config
        from agent_core.llm.provider_resolve import resolve_llm_provider_key

        cfg = get_config()
        name = resolve_llm_provider_key(cfg.llm, query)
        prov_map = getattr(cfg.llm, "providers", {}) or {}
        prov = prov_map.get(name)
        if prov is None:
            raise ValueError(
                f"未知 provider: {query}；已注册：{list(prov_map.keys())}"
            )
        caps_m = getattr(prov, "capabilities", None)
        vision_vp = self._resolve_vision_provider_name_static(cfg.llm, prov_map)
        return {
            "name": name,
            "model": prov.model,
            "api_model": prov.model,
            "vision": bool(getattr(caps_m, "vision", False)) if caps_m else False,
            "vision_provider": vision_vp,
        }

    def switch_model(
        self, name: str, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """切换 session 的主对话 LLM provider。

        首选存在 ``CorePool`` 中：无 Core 时只更新池内记录，首轮 ``acquire`` 创建
        AgentCore 后在 ``_load`` 里应用；已有 Core 时同时更新 ``LLMClient``。
        """
        sid = session_id or self._active_session_id
        name = str(name).strip()
        if not name:
            raise ValueError("name 不能为空")

        pool = self._kernel_scheduler.core_pool
        pool.set_session_preferred_llm_provider(sid, name)

        live = pool.get_live_entry(sid)
        if live is not None and getattr(live, "agent", None) is not None:
            fn = getattr(live.agent, "switch_model", None)
            if not callable(fn):
                raise RuntimeError("当前 agent 不支持运行时 switch_model")
            result = fn(name)
            if not isinstance(result, dict):
                return {"name": str(name)}
            return result
        return self._switch_result_from_config_provider(name)

    def get_turn_count(self, session_id: Optional[str] = None) -> int:
        sid = session_id or self._active_session_id
        entry = self._kernel_scheduler.core_pool.get_entry(sid)
        if entry is None:
            return 0
        fn = getattr(entry.agent, "get_turn_count", None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:
                return 0
        return 0

    async def delete_session(self, session_id: str) -> bool:
        """删除指定会话。

        - 删除 ChatHistoryDB 中该 session 的历史消息（如可用）
        - 关闭并移除内存中的 CoreSession（如有）
        - 从 SessionRegistry 中删除该会话记录
        - 不删除长期记忆（LongTermMemory），仅清理对话历史

        为避免当前交互状态混乱，不允许删除当前 active_session。
        """
        sid = (session_id or "").strip()
        if not sid:
            return False
        if sid == self._active_session_id:
            # 不直接删除当前活跃会话，避免前端仍在使用时状态不一致。
            return False

        existed_in_memory = sid in self._sessions
        existed_in_registry = self._session_registry.session_exists(
            self._owner_id, self._source, sid
        )
        # 既不在内存也不在注册表中，视为不存在的会话，直接返回失败，避免误报“删除成功”。
        if not existed_in_memory and not existed_in_registry:
            return False

        # 确保有一个 CoreSession 用于执行历史删除；对于未加载的冷会话，通过 session_factory 创建临时实例。
        session = self._sessions.get(sid)
        created_temp = False
        if (
            session is None
            and existed_in_registry
            and self._session_factory is not None
        ):
            created = self._session_factory(sid)
            session = await created if inspect.isawaitable(created) else created
            created_temp = True

        async def _close_session_if_needed(
            target: CoreSession | None, *, temp: bool
        ) -> None:
            if target is None:
                return
            close = getattr(target, "close", None)
            if not callable(close):
                return
            try:
                maybe = close()
                if inspect.isawaitable(maybe):
                    await maybe
            except Exception as exc:
                if temp:
                    logger.warning(
                        "close temp session failed during delete (session_id=%s): %s",
                        sid,
                        exc,
                    )
                else:
                    logger.warning(
                        "close session failed during delete (session_id=%s): %s",
                        sid,
                        exc,
                    )

        # 没有可用 CoreSession 时，无法保证历史已被删除；直接失败，避免“元数据删除但历史残留”。
        if session is None:
            logger.warning(
                "delete_session aborted: no core session available (session_id=%s)", sid
            )
            return False

        # 优先删除 ChatHistoryDB 中该 session 的历史；仅当删除动作成功时继续删除注册表元数据。
        delete_history = getattr(session, "delete_session_history", None)
        if not callable(delete_history):
            logger.warning(
                "delete_session aborted: delete_session_history is unavailable (session_id=%s)",
                sid,
            )
            if created_temp:
                await _close_session_if_needed(session, temp=True)
            return False
        try:
            maybe = delete_history(sid)
            if inspect.isawaitable(maybe):
                await maybe
        except Exception as exc:
            logger.warning(
                "delete_session_history failed (session_id=%s): %s", sid, exc
            )
            if created_temp:
                await _close_session_if_needed(session, temp=True)
            return False

        # 若 session 在 CorePool 中，需 evict 移除
        try:
            await self._kernel_scheduler.core_pool.evict(sid)
        except Exception:
            pass
        # 若是 Gateway 本地持有的会话，从管理结构中移除并关闭
        if sid in self._sessions:
            owned = sid in self._owned_sessions
            session = self._sessions.pop(sid, None)
            self._last_activity.pop(sid, None)
            if owned:
                self._owned_sessions.discard(sid)
            await _close_session_if_needed(session, temp=False)
        elif created_temp:
            await _close_session_if_needed(session, temp=True)

        self._session_registry.delete_session(self._owner_id, self._source, sid)
        self._kernel_scheduler.core_pool.clear_session_preferred_llm_provider(sid)
        return True

    async def close(self) -> None:
        # 取消所有 OutputBus 订阅
        for session_id, sub_id in list(self._subscriptions.items()):
            try:
                self._kernel_scheduler.unsubscribe_out(session_id, sub_id)
            except Exception:
                pass
        self._subscriptions.clear()
        self._push_buffers.clear()
        # 只关闭 gateway 自身创建的 session（_owned_sessions）；
        # 构造函数传入的初始 session 由调用方持有，gateway 不拥有它的生命周期。
        for session_id in list(self._owned_sessions):
            session = self._sessions.get(session_id)
            if session is None:
                continue
            try:
                await session.close()
            except Exception as exc:
                logger.warning(
                    "close owned session failed (session_id=%s): %s", session_id, exc
                )
            finally:
                self._sessions.pop(session_id, None)
                self._last_activity.pop(session_id, None)
                self._owned_sessions.discard(session_id)
        self._session_registry.close()
