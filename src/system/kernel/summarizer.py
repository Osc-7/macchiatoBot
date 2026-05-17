"""
SessionSummarizer — Kill 流程的摘要器。

Kernel 在 evict 一个 Core 之后调用此组件：
1. 接收 CoreStatsAction（token 用量、turn count、session 起止时间）
2. 可选接收该 session 的对话消息列表
3. 调用 LLM 生成摘要
4. 将摘要写入对应前端的长期记忆（LongTermMemory.add_recent_topic）

设计原则：
- 纯函数风格，不持有任何 Core/Session 状态
- 允许不传 messages（退化为仅基于 CoreStats 记录资源消耗）
- 失败时记录日志但不抛异常，保证 evict 流程不因摘要失败而卡住
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from system.kernel.summary_prompt import SUMMARY_USER_APPEND

if TYPE_CHECKING:
    from agent_core.kernel_interface.action import CoreStatsAction
    from agent_core.llm import LLMClient

logger = logging.getLogger(__name__)


class SessionSummarizer:
    """
    Session 摘要器 — Kernel 专用。

    在 Core 被 kill 后，由 CorePool.evict() 调用，
    生成本次 session 的摘要并持久化到长期记忆。

    Usage::

        summarizer = SessionSummarizer(llm_client=llm_client)
        await summarizer.summarize_and_persist(
            stats=core_stats_action,
            long_term_memory=agent.long_term_memory,
            messages=agent.context.get_messages(),
        )
    """

    def __init__(self, llm_client: Optional["LLMClient"] = None) -> None:
        self._llm_client = llm_client

    async def summarize_and_persist(
        self,
        stats: "CoreStatsAction",
        long_term_memory: Any,
        messages: Optional[List[Dict[str, Any]]] = None,
        owner_id: Optional[str] = None,
        system_message: Optional[str] = None,
    ) -> Optional[str]:
        """
        生成摘要并写入长期记忆。

        Args:
            stats:             CoreStatsAction，包含 token_usage / turn_count / session_id
            long_term_memory:  LongTermMemory 实例，具有 add_recent_topic() 方法
            messages:          该 session 的完整对话消息列表（可选，有则生成语义摘要）
            owner_id:          记忆所有者 ID（水源等多用户场景需要）
            system_message:    与该 Core 正常对话一致的 system prompt，用于提高 prefix cache 命中率

        Returns:
            生成的摘要文本，若跳过则返回 None。
        """
        try:
            summary_text = await self._generate_summary(
                stats, messages, system_message=system_message
            )
            if summary_text and long_term_memory is not None:
                add_recent = getattr(long_term_memory, "add_recent_topic", None)
                if callable(add_recent):
                    add_recent(
                        summary=summary_text,
                        session_id=stats.session_id,
                        tags=self._extract_tags(stats),
                        owner_id=owner_id,
                    )
                    logger.info(
                        "SessionSummarizer: persisted summary for session=%s (%d turns, %d tokens)",
                        stats.session_id,
                        stats.turn_count,
                        stats.token_usage.get("total_tokens", 0),
                    )
            return summary_text
        except Exception as exc:
            logger.warning(
                "SessionSummarizer: failed for session=%s: %s", stats.session_id, exc
            )
            return None

    async def _generate_summary(
        self,
        stats: "CoreStatsAction",
        messages: Optional[List[Dict[str, Any]]],
        *,
        system_message: Optional[str] = None,
    ) -> str:
        """调用 LLM 生成摘要，无 LLM 则退化为结构化文本摘要。"""
        if stats.turn_count == 0:
            return ""

        if not messages or self._llm_client is None:
            return self._fallback_summary(stats)

        chat_messages = [dict(m) for m in messages]
        if not chat_messages:
            return self._fallback_summary(stats)
        chat_messages.append(
            {
                "role": "user",
                "content": SUMMARY_USER_APPEND,
            }
        )

        try:
            response = await self._llm_client.chat(
                system_message=(system_message or "").strip(),
                messages=chat_messages,
            )
            return (
                response.content.strip()
                if response.content
                else self._fallback_summary(stats)
            )
        except Exception as exc:
            logger.warning("SessionSummarizer: LLM call failed: %s", exc)
            return self._fallback_summary(stats)

    @staticmethod
    def _fallback_summary(stats: "CoreStatsAction") -> str:
        """无 LLM 时的退化摘要，仅记录统计信息。"""
        tokens = stats.token_usage.get("total_tokens", 0)
        return (
            f"[自动摘要] session={stats.session_id}, "
            f"turns={stats.turn_count}, tokens={tokens}, "
            f"start={stats.session_start_time}"
        )

    @staticmethod
    def _extract_tags(stats: "CoreStatsAction") -> List[str]:
        """从 CoreStatsAction 提取简单标签（供长期记忆索引）。"""
        return [f"session:{stats.session_id[:12]}"]
