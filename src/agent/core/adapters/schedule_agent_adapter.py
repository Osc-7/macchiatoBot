"""ScheduleAgent 到 CoreSession 的适配器。"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Dict, List, Optional

from agent.content import ContentReference, resolve_content_refs
from agent.core.interfaces import (
    AgentHooks,
    AgentRunInput,
    AgentRunResult,
    AgentSessionState,
    CoreEvent,
)


class ScheduleAgentAdapter:
    """将现有 ScheduleAgent 映射为稳定 CoreSession 接口。"""

    def __init__(self, agent: Any):
        self._agent = agent
        self._agent_started = False

    async def run_turn(
        self,
        agent_input: AgentRunInput,
        hooks: Optional[AgentHooks] = None,
    ) -> AgentRunResult:
        hooks = hooks or AgentHooks()
        if not self._agent_started:
            await self._emit_event(hooks, CoreEvent(name="agent_start"))
            self._agent_started = True

        # 若底层 Agent 使用 defer_mcp_connect，在首轮或后续轮次前确保 MCP 已连接
        ensure_mcp = getattr(self._agent, "ensure_mcp_connected", None)
        if callable(ensure_mcp):
            maybe = ensure_mcp()
            if inspect.isawaitable(maybe):
                await maybe

        await self._emit_event(hooks, CoreEvent(name="turn_start"))

        async def on_stream_delta(delta: str) -> None:
            if hooks.on_assistant_delta:
                maybe = hooks.on_assistant_delta(delta)
                if inspect.isawaitable(maybe):
                    await maybe
            await self._emit_event(
                hooks,
                CoreEvent(name="assistant_delta", payload={"delta": delta}),
            )

        async def on_reasoning_delta(delta: str) -> None:
            if hooks.on_reasoning_delta:
                maybe = hooks.on_reasoning_delta(delta)
                if inspect.isawaitable(maybe):
                    await maybe
            await self._emit_event(
                hooks,
                CoreEvent(name="reasoning_delta", payload={"delta": delta}),
            )

        async def on_trace_event(event: Dict[str, Any]) -> None:
            if hooks.on_trace_event:
                maybe = hooks.on_trace_event(event)
                if inspect.isawaitable(maybe):
                    await maybe
            mapped = self._map_trace_event(event)
            if mapped is not None:
                await self._emit_event(hooks, mapped)

        # 解析 content_refs（如飞书图片/视频）为 LLM content items
        content_items: List[Dict[str, Any]] = []
        raw_refs = agent_input.metadata.get("content_refs")
        if isinstance(raw_refs, list) and raw_refs:
            refs = [ContentReference.from_dict(r) for r in raw_refs]
            try:
                content_items = await resolve_content_refs(refs)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "content_refs resolve failed: %s", exc
                )

        try:
            output = await self._agent.process_input(
                agent_input.text,
                content_items=content_items,
                on_stream_delta=on_stream_delta,
                on_reasoning_delta=on_reasoning_delta,
                on_trace_event=on_trace_event,
            )
            await self._emit_event(
                hooks,
                CoreEvent(name="assistant_final", payload={"content": output}),
            )
            await self._emit_event(hooks, CoreEvent(name="turn_end"))
            attachments = getattr(self._agent, "get_outgoing_attachments", lambda: [])()
            return AgentRunResult(output_text=output, attachments=attachments)
        except Exception as exc:
            await self._emit_event(
                hooks,
                CoreEvent(name="agent_error", payload={"error": str(exc)}),
            )
            raise

    async def finalize_session(self):
        return await self._agent.finalize_session()

    def reset_session(self) -> None:
        self._agent.reset_session()

    async def close(self) -> None:
        await self._agent.close()

    async def activate_session(
        self,
        session_id: str,
        replay_messages_limit: Optional[int] = None,
    ) -> None:
        activate = getattr(self._agent, "activate_session", None)
        if callable(activate):
            maybe = activate(session_id, replay_messages_limit=replay_messages_limit)
            if inspect.isawaitable(maybe):
                await maybe

    def get_session_state(self) -> AgentSessionState:
        session_id = getattr(self._agent, "_session_id", "")
        turn_count = self._agent.get_turn_count()
        token_usage = self._agent.get_token_usage()
        return AgentSessionState(
            session_id=session_id,
            turn_count=turn_count,
            token_usage=token_usage,
        )

    def clear_context(self) -> None:
        self._agent.clear_context()

    def get_token_usage(self) -> dict:
        return self._agent.get_token_usage()

    def get_turn_count(self) -> int:
        return self._agent.get_turn_count()

    def delete_session_history(self, session_id: str) -> int:
        fn = getattr(self._agent, "delete_session_history", None)
        if callable(fn):
            return fn(session_id)
        return 0

    @property
    def config(self):
        return self._agent.config

    @property
    def raw_agent(self) -> Any:
        return self._agent

    def _map_trace_event(self, event: Dict[str, Any]) -> CoreEvent | None:
        event_type = event.get("type")
        if event_type == "llm_request":
            return CoreEvent(name="llm_request", payload=event)
        if event_type in {"tool_call", "tool_result"}:
            return CoreEvent(name=event_type, payload=event)
        return None

    async def _emit_event(self, hooks: AgentHooks, event: CoreEvent) -> None:
        if not hooks.on_event:
            return
        maybe = hooks.on_event(event)
        if inspect.isawaitable(maybe):
            await maybe
