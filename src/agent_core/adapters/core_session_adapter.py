"""AgentCore 到 CoreSession 的适配器。"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Dict, List, Optional, cast

from agent_core.content import ContentReference, resolve_content_refs
from agent_core.interfaces import (
    AgentHooks,
    AgentRunInput,
    AgentSessionState,
    CoreEvent,
)
from agent_core.interfaces.models import AgentRunResult
from system.kernel import AgentKernel

logger = logging.getLogger(__name__)


class CoreSessionAdapter:
    """
    将现有 AgentCore 映射为稳定 CoreSession 接口。

    run_turn() 通过 AgentKernel 驱动 agent.run_loop()：
    - AgentCore 直接持有 LLMClient，在 run_loop() 内自旋完成多轮 LLM 推理（无 Kernel 中介）
    - 只有工具调用（ToolCallAction）和返回（ReturnAction）会 yield 到 Kernel，由 Kernel 统一执行工具 IO
    """

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

        # 若底层 Agent 使用 defer_mcp_connect，首轮前确保 MCP 已连接
        ensure_mcp = getattr(self._agent, "ensure_mcp_connected", None)
        if callable(ensure_mcp):
            maybe = ensure_mcp()
            if inspect.isawaitable(maybe):
                await maybe

        await self._emit_event(hooks, CoreEvent(name="turn_start"))

        # 优先使用前端已 resolve 好的 content_items（避免 daemon 进程缺少对应 resolver）
        content_items: List[Dict[str, Any]] = []
        pre_resolved = agent_input.metadata.get("content_items") if agent_input.metadata else None
        if isinstance(pre_resolved, list) and pre_resolved:
            content_items = pre_resolved
            logger.info(
                "core_session_adapter: using pre-resolved content_items count=%d first_types=%s",
                len(content_items),
                [str(i.get("type")) for i in content_items[:3]],
            )
        else:
            raw_refs = (
                agent_input.metadata.get("content_refs") if agent_input.metadata else None
            )
            if isinstance(raw_refs, list) and raw_refs:
                refs = [ContentReference.from_dict(r) for r in raw_refs]
                try:
                    content_items = await resolve_content_refs(refs)
                    logger.info(
                        "core_session_adapter: resolved %d content_items from %d refs",
                        len(content_items),
                        len(refs),
                    )
                except Exception as exc:
                    logger.warning("content_refs resolve failed: %s", exc)

        # 包装 hooks，在流式回调中同时派发 CoreEvent
        wrapped_hooks = self._wrap_hooks_with_events(hooks)

        run_result = None
        try:
            run_result = await self._run_via_kernel(
                agent_input, content_items, wrapped_hooks
            )

            await self._emit_event(
                hooks,
                CoreEvent(
                    name="assistant_final", payload={"content": run_result.output_text}
                ),
            )
            await self._emit_event(hooks, CoreEvent(name="turn_end"))

            attachments = run_result.attachments or []
            if not attachments:
                attachments = getattr(
                    self._agent, "get_outgoing_attachments", lambda: []
                )()
            return AgentRunResult(
                output_text=run_result.output_text, attachments=attachments
            )

        except Exception as exc:
            await self._emit_event(
                hooks,
                CoreEvent(name="agent_error", payload={"error": str(exc)}),
            )
            raise
        finally:
            # SessionManager 路径：记录 turn_end（Scheduler 路径在 scheduler 内调用）
            sl = getattr(self._agent, "_session_logger", None)
            if sl is not None and hasattr(sl, "on_turn_end"):
                try:
                    turn_id = getattr(self._agent, "_current_turn_id", 0)
                    output = run_result.output_text if run_result else ""
                    meta = dict(run_result.metadata) if run_result and run_result.metadata else {}
                    if run_result is None:
                        meta["error"] = "run failed before result"
                    maybe = sl.on_turn_end(
                        turn_id,
                        output_text=output,
                        metadata=meta if meta else None,
                        request_id="",
                    )
                    if inspect.isawaitable(maybe):
                        await maybe
                except Exception:
                    pass

    async def _run_via_kernel(
        self,
        agent_input: AgentRunInput,
        content_items: List[Dict[str, Any]],
        hooks: AgentHooks,
    ) -> AgentRunResult:
        """新架构路径：通过 AgentKernel 驱动 run_loop()。"""
        input_text = agent_input.text

        # 前置处理：同步外部更新、memory recall、写入用户消息（统一路径）
        turn_id = await self._agent.prepare_turn(input_text, content_items)

        # SessionManager 路径：记录 turn_start（Scheduler 路径在 scheduler 内调用）
        sl = getattr(self._agent, "_session_logger", None)
        if sl is not None and hasattr(sl, "on_turn_start"):
            try:
                maybe = sl.on_turn_start(turn_id, input_text, request_id="")
                if inspect.isawaitable(maybe):
                    await maybe
            except Exception:
                pass

        # AgentKernel 驱动 run_loop()（Kernel 只需 ToolRegistry，LLM 由 AgentCore 直接调用）
        kernel = AgentKernel(tool_registry=self._agent._tool_registry)
        run_result = None
        try:
            run_result = await kernel.run(self._agent, turn_id=turn_id, hooks=hooks)
        finally:
            await self._agent._finalize_turn(run_result)
        return run_result

    def _wrap_hooks_with_events(self, hooks: AgentHooks) -> AgentHooks:
        """
        返回一个新 AgentHooks，在原有回调基础上额外派发 CoreEvent。

        确保流式 delta 和 trace event 都能触发 hooks.on_event。
        """
        adapter = self

        async def on_assistant_delta(delta: str) -> None:
            if hooks.on_assistant_delta:
                maybe = hooks.on_assistant_delta(delta)
                if inspect.isawaitable(maybe):
                    await maybe
            await adapter._emit_event(
                hooks, CoreEvent(name="assistant_delta", payload={"delta": delta})
            )

        async def on_reasoning_delta(delta: str) -> None:
            if hooks.on_reasoning_delta:
                maybe = hooks.on_reasoning_delta(delta)
                if inspect.isawaitable(maybe):
                    await maybe
            await adapter._emit_event(
                hooks, CoreEvent(name="reasoning_delta", payload={"delta": delta})
            )

        async def on_trace_event(event: Dict[str, Any]) -> None:
            if hooks.on_trace_event:
                maybe = hooks.on_trace_event(event)
                if inspect.isawaitable(maybe):
                    await maybe
            mapped = adapter._map_trace_event(event)
            if mapped is not None:
                await adapter._emit_event(hooks, mapped)

        return AgentHooks(
            on_assistant_delta=on_assistant_delta,
            on_reasoning_delta=on_reasoning_delta,
            on_trace_event=on_trace_event,
            on_event=hooks.on_event,
        )

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
            return cast(int, fn(session_id))
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
