"""System prompt assembly for AgentCore."""

from __future__ import annotations

from typing import Any, List

from agent_core.memory import RecallResult
from agent_core.prompts.loader import (
    PromptMode,
    build_system_prompt as build_prompt,
    get_recipe,
    resolve_skills_cli_path,
)


def _visible_scopes(agent: Any) -> set:
    """返回当前 Core 可见的记忆 scope 集合；无 profile 时视为全部可见（向后兼容）。"""
    profile = getattr(agent, "_core_profile", None)
    if profile is None:
        return {"working", "long_term", "content", "chat"}
    scopes = getattr(profile, "visible_memory_scopes", None) or []
    return set(scopes)


def _loader_prompt_mode(agent: Any) -> PromptMode:
    """映射 CoreProfile.mode → build_system_prompt 的 mode。

    - full / sub：全量片段（identity、soul、Workspace 引导等）；子 Agent 与主会话一致。
    - background：minimal（time → safety → tools + 条件 runtime），省 token。
    """
    profile = getattr(agent, "_core_profile", None)
    if profile is None:
        return "full"
    m = getattr(profile, "mode", None) or "full"
    if m == "background":
        return "minimal"
    return "full"


def build_agent_system_prompt(agent: Any) -> str:
    """Build the current system prompt from agent runtime state.

    ``build_system_prompt`` 的 ``mode`` 由 ``CoreProfile.mode`` 决定：``full`` 与 ``sub`` 均用
    全量片段；仅 ``background`` 用 ``minimal``（无人设块与 Workspace 引导文件段）。

    工作记忆不再单独注入 system：会话状态即 ``ConversationContext.messages`` 滑动窗口
    （含 Kernel 折叠产生的 ``[会话进行中摘要]`` user 条）；长期记忆等仍见「# 记忆上下文」。
    """
    scopes = _visible_scopes(agent)

    recipe = get_recipe(getattr(agent, "_source", "cli") or "cli")
    skills_cli = resolve_skills_cli_path(
        agent._config,
        source=getattr(agent, "_source", "cli") or "cli",
        user_id=getattr(agent, "_user_id", "root") or "root",
        profile=getattr(agent, "_core_profile", None),
    )
    prompt = build_prompt(
        config=agent._config,
        has_web_extractor=agent._tool_registry.has("extract_web_content"),
        has_file_tools=agent._tool_registry.has("read_file"),
        mode=_loader_prompt_mode(agent),
        recipe=recipe,
        skills_cli_path=skills_cli,
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

    # automation 摘要：由 PromptRecipe.include_digest 与 long_term scope 共同控制
    digest_sections: List[str] = []
    daily_digest = None
    weekly_digest = None
    if recipe.include_digest and "long_term" in scopes:
        try:
            from system.automation.repositories import DigestRepository  # type: ignore[import]

            digest_repo = DigestRepository()
            daily_digest = digest_repo.latest("daily")
            weekly_digest = digest_repo.latest("weekly")
        except Exception:
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

    if recipe.include_digest and digest_sections:
        prompt += "\n\n# 自动化摘要\n\n" + "\n".join(digest_sections)

    return prompt
