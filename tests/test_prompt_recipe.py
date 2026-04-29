"""PromptRecipe 与 build_system_prompt 渠道配方测试。"""

from agent_core.config import Config, LLMConfig, SkillsConfig
from agent_core.prompts.loader import (
    PromptRecipe,
    build_system_prompt,
    get_recipe,
)


def _minimal_config() -> Config:
    return Config(
        llm=LLMConfig(api_key="k", model="m"),
        skills=SkillsConfig(enabled=[], cli_dir=None),
    )


class TestGetRecipe:
    def test_shuiyuan_recipe(self):
        r = get_recipe("shuiyuan")
        assert r.identity is None
        assert r.soul is None
        assert r.channel_overlay == "shuiyuan/system"
        assert r.workspace_sections == ("multi_agent",)
        assert r.include_skills is True
        assert r.include_digest is False

    def test_default_for_unknown_source(self):
        r = get_recipe("feishu")
        assert r.identity == "system/identity"
        assert r.channel_overlay is None
        assert "agents" in r.workspace_sections
        assert "multi_agent" in r.workspace_sections
        assert "schedule" in r.workspace_sections
        assert r.include_digest is True


class TestBuildSystemPromptRecipes:
    def test_default_includes_identity_agents_schedule_multi_agent(self):
        cfg = _minimal_config()
        p = build_system_prompt(
            config=cfg,
            has_web_extractor=False,
            mode="full",
            recipe=PromptRecipe(),
        )
        assert "# IDENTITY" in p
        assert "# SOUL" in p
        assert "# AGENTS" in p
        assert "# MULTI-AGENT" in p or "MULTI-AGENT" in p
        assert "# 日程与任务操作规范" in p

    def test_shuiyuan_has_overlay_and_multi_agent_not_schedule(self):
        cfg = _minimal_config()
        p = build_system_prompt(
            config=cfg,
            has_web_extractor=False,
            mode="full",
            recipe=get_recipe("shuiyuan"),
        )
        assert "# 水源社区 Agent" in p
        assert "# MULTI-AGENT" in p or "MULTI-AGENT" in p
        assert "# 日程与任务操作规范" not in p
        assert "# AGENTS" not in p
        assert "# IDENTITY" not in p

    def test_shuiyuan_recipe_skills_off_still_loads_workspace_header_for_multi_agent_only(
        self,
    ):
        cfg = _minimal_config()
        r = PromptRecipe(
            identity=None,
            soul=None,
            channel_overlay="shuiyuan/system",
            workspace_sections=("multi_agent",),
            include_skills=False,
            include_digest=False,
        )
        p = build_system_prompt(
            config=cfg,
            has_web_extractor=False,
            mode="full",
            recipe=r,
        )
        assert "# Workspace Files" in p
        assert "# MULTI-AGENT" in p or "MULTI-AGENT" in p
        assert "## Available Skills (Index)" not in p
