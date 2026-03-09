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
    ) -> Optional[str]:
        """
        生成摘要并写入长期记忆。

        Args:
            stats:             CoreStatsAction，包含 token_usage / turn_count / session_id
            long_term_memory:  LongTermMemory 实例，具有 add_recent_topic() 方法
            messages:          该 session 的完整对话消息列表（可选，有则生成语义摘要）
            owner_id:          记忆所有者 ID（水源等多用户场景需要）

        Returns:
            生成的摘要文本，若跳过则返回 None。
        """
        try:
            summary_text = await self._generate_summary(stats, messages)
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
    ) -> str:
        """调用 LLM 生成摘要，无 LLM 则退化为结构化文本摘要。"""
        if stats.turn_count == 0:
            return ""

        if not messages or self._llm_client is None:
            return self._fallback_summary(stats)

        # 过滤出 user/assistant 消息，避免把大量 tool_result 全部送入摘要 LLM
        dialogue = [
            m for m in messages
            if m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
            and m["content"].strip()
        ]

        if not dialogue:
            return self._fallback_summary(stats)

        dialogue_text = "\n".join(
            f"[{m['role']}]: {m['content'][:500]}"
            for m in dialogue[-30:]  # 最多取最近 30 条，控制摘要 LLM 输入
        )

        prompt = (
            f"请用 2-4 句话概括以下对话的核心内容、达成的决定和待跟进事项。"
            f"对话共 {stats.turn_count} 轮，token 消耗 {stats.token_usage.get('total_tokens', 0)}。\n\n"
            f"{dialogue_text}"
        )

        try:
            response = await self._llm_client.chat(
                system_message="你是一个高效的会话摘要助手，输出简洁的中文摘要。",
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content.strip() if response.content else self._fallback_summary(stats)
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
