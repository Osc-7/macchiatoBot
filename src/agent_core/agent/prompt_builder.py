"""System prompt assembly for AgentCore."""

from __future__ import annotations

from typing import Any, List

from agent_core.context import get_time_context
from agent_core.memory import RecallResult
from agent_core.prompts import build_system_prompt as build_prompt


def _visible_scopes(agent: Any) -> set:
    """返回当前 Core 可见的记忆 scope 集合；无 profile 时视为全部可见（向后兼容）。"""
    profile = getattr(agent, "_core_profile", None)
    if profile is None:
        return {"working", "long_term", "content", "chat"}
    scopes = getattr(profile, "visible_memory_scopes", None) or []
    return set(scopes)


def build_agent_system_prompt(agent: Any) -> str:
    """Build the current system prompt from agent runtime state.

    工作记忆不再单独注入 system：会话状态即 ``ConversationContext.messages`` 滑动窗口
    （含 Kernel 折叠产生的 ``[会话进行中摘要]`` user 条）；长期记忆等仍见「# 记忆上下文」。
    """
    time_ctx = get_time_context(agent._timezone)
    time_str = time_ctx.to_prompt_string()
    scopes = _visible_scopes(agent)

    sub_content_path = "shuiyuan/system" if agent._source == "shuiyuan" else None
    prompt = build_prompt(
        time_context=time_str,
        config=agent._config,
        has_web_extractor=agent._tool_registry.has("extract_web_content"),
        has_file_tools=agent._tool_registry.has("read_file"),
        sub_content_path=sub_content_path,
    )

    if agent._memory_enabled:
        parts: List[str] = []
        # long_term: recent_topics + MEMORY.md
        if "long_term" in scopes:
            owner_id = None
            if getattr(agent, "_source", "") == "shuiyuan":
                # 水源多用户前端：按用户名拆分最近话题，避免不同源友的长期记忆串线
                owner_id = getattr(agent, "_user_id", None)
            recent_topics = agent._long_term_memory.get_recent_topics(
                agent._config.memory.recall_top_n,
                owner_id=owner_id,
            )
            if recent_topics:
                parts.append("## 最近话题")
                for topic in recent_topics:
                    ts = topic.created_at[:10] if topic.created_at else ""
                    ts_prefix = f"[{ts}] " if ts else ""
                    parts.append(f"- {ts_prefix}{topic.content}")
            md_content = agent._long_term_memory.read_memory_md()
            if md_content and len(md_content) > 50:
                excerpt = (
                    md_content
                    if len(md_content) <= 1000
                    else md_content[:1000] + "\n..."
                )
                parts.append("\n## 核心记忆 (MEMORY.md)")
                parts.append(excerpt)
        # long_term / content: recall 结果
        if any(s in scopes for s in ("long_term", "content")):
            recall_ctx = getattr(agent, "_last_recall_result", RecallResult())
            recall_text = recall_ctx.to_context_string()
            if recall_text:
                parts.append(f"\n{recall_text}")
        if parts:
            prompt += "\n\n# 记忆上下文\n\n" + "\n".join(parts)

    # automation 摘要：仅在可见 long_term 时注入（作为辅助上下文）
    digest_sections: List[str] = []
    if "long_term" in scopes:
        try:
            from system.automation.repositories import DigestRepository  # type: ignore[import]

            digest_repo = DigestRepository()
            daily_digest = digest_repo.latest("daily")
            weekly_digest = digest_repo.latest("weekly")
        except Exception:
            daily_digest = None
            weekly_digest = None
    else:
        daily_digest = None
        weekly_digest = None

    if daily_digest is not None:
        digest_sections.append("## 最近日摘要")
        for item in (daily_digest.highlights or [])[:5]:
            digest_sections.append(f"- {item}")
        if daily_digest.content_md:
            content = daily_digest.content_md
            max_len = 800
            excerpt = (
                content if len(content) <= max_len else content[:max_len] + "\n..."
            )
            digest_sections.append("")
            digest_sections.append(excerpt)

    if weekly_digest is not None:
        if digest_sections:
            digest_sections.append("")
        digest_sections.append("## 最近周摘要")
        for item in (weekly_digest.highlights or [])[:5]:
            digest_sections.append(f"- {item}")
        if weekly_digest.content_md:
            content = weekly_digest.content_md
            max_len = 800
            excerpt = (
                content if len(content) <= max_len else content[:max_len] + "\n..."
            )
            digest_sections.append("")
            digest_sections.append(excerpt)

    if digest_sections:
        prompt += "\n\n# 自动化摘要\n\n" + "\n".join(digest_sections)

    return prompt
