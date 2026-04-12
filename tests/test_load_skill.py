"""
LoadSkillTool 与 loader 渐进式披露相关测试
"""

from pathlib import Path

import pytest

from agent_core.config import CommandToolsConfig, Config, LLMConfig, SkillsConfig
from system.tools.load_skill_tool import LoadSkillTool
from agent_core.prompts.loader import (
    _format_skills_index,
    _parse_skill_frontmatter,
    build_system_prompt,
    load_skill_content,
)


class TestParseSkillFrontmatter:
    def test_valid_frontmatter(self):
        content = """---
name: 我的技能
description: 这是一个测试技能
---
正文内容
"""
        name, desc = _parse_skill_frontmatter(content)
        assert name == "我的技能"
        assert desc == "这是一个测试技能"

    def test_empty_frontmatter(self):
        content = """---
---
正文
"""
        name, desc = _parse_skill_frontmatter(content)
        assert name is None
        assert desc is None

    def test_no_frontmatter(self):
        content = "纯正文，无 frontmatter"
        name, desc = _parse_skill_frontmatter(content)
        assert name is None
        assert desc is None


class TestFormatSkillsIndex:
    def test_empty_enabled(self):
        assert _format_skills_index([]) == ""

    def test_skill_not_found_skipped(self):
        # 无 cli_dir 时 index 为空
        assert _format_skills_index(["nonexistent-skill"], cli_dir_path=None) == ""


class TestLoadSkillContent:
    def test_nonexistent_skill_returns_empty(self):
        assert load_skill_content("definitely-not-a-skill") == ""

    def test_example_skill_returns_content(self, tmp_path):
        ex = tmp_path / "example"
        ex.mkdir()
        (ex / "SKILL.md").write_text(
            "---\nname: 示例技能\ndescription: d\n---\n\n使用说明\n",
            encoding="utf-8",
        )
        content = load_skill_content("example", cli_dir_path=tmp_path)
        assert "示例技能" in content or "使用说明" in content


class TestLoadSkillTool:
    @pytest.fixture
    def config_with_skills(self):
        return Config(
            llm=LLMConfig(api_key="k", model="m"),
            skills__enabled=["my-skill"],
        )

    def test_get_definition(self):
        cfg = Config(
            llm=LLMConfig(api_key="k", model="m"),
            skills=SkillsConfig(enabled=["my-skill"]),
        )
        tool = LoadSkillTool(config=cfg)
        defn = tool.get_definition()
        assert defn.name == "load_skill"
        assert "skill_name" in [p.name for p in defn.parameters]

    @pytest.mark.asyncio
    async def test_execute_empty_skill_name(self):
        cfg = Config(
            llm=LLMConfig(api_key="k", model="m"),
            skills=SkillsConfig(enabled=[]),
        )
        tool = LoadSkillTool(config=cfg)
        r = await tool.execute(skill_name="")
        assert r.success is False
        assert r.error == "INVALID_ARGUMENTS"

    @pytest.mark.asyncio
    async def test_execute_skill_not_found(self):
        cfg = Config(
            llm=LLMConfig(api_key="k", model="m"),
            skills=SkillsConfig(enabled=["enabled-skill"]),
        )
        tool = LoadSkillTool(config=cfg)
        r = await tool.execute(skill_name="nonexistent-skill-xyz")
        assert r.success is False
        assert r.error == "SKILL_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_execute_loads_full_content(self, tmp_path):
        ex = tmp_path / "example"
        ex.mkdir()
        (ex / "SKILL.md").write_text(
            "---\nname: 示例技能\ndescription: d\n---\n\n使用说明\n",
            encoding="utf-8",
        )
        cfg = Config(
            llm=LLMConfig(api_key="k", model="m"),
            skills=SkillsConfig(enabled=["example"], cli_dir=str(tmp_path)),
            command_tools=CommandToolsConfig(
                base_dir=str(tmp_path),
                workspace_isolation_enabled=False,
            ),
        )
        tool = LoadSkillTool(config=cfg)
        r = await tool.execute(skill_name="example")
        assert r.success is True
        assert "示例技能" in r.message or "使用说明" in r.message

    @pytest.mark.asyncio
    async def test_execute_loads_from_workspace_dot_agents_when_isolated(self, tmp_path):
        """隔离模式下与 bash 一致：~/.agents/skills → 用户单元格下 .agents/skills。"""
        ws_root = tmp_path / "workspace_parent"
        owner_skills = (
            ws_root / "feishu" / "u9" / ".agents" / "skills" / "example"
        )
        owner_skills.mkdir(parents=True)
        (owner_skills / "SKILL.md").write_text(
            "---\nname: 示例技能\ndescription: d\n---\n\n隔离体\n",
            encoding="utf-8",
        )
        cfg = Config(
            llm=LLMConfig(api_key="k", model="m"),
            skills=SkillsConfig(enabled=["example"]),
            command_tools=CommandToolsConfig(
                base_dir=str(tmp_path),
                workspace_base_dir=str(ws_root),
                workspace_isolation_enabled=True,
            ),
        )
        tool = LoadSkillTool(config=cfg)
        r = await tool.execute(
            skill_name="example",
            __execution_context__={
                "source": "feishu",
                "user_id": "u9",
                "bash_workspace_admin": False,
            },
        )
        assert r.success is True
        assert "隔离体" in r.message


class TestBuildSystemPromptSkills:
    def test_skills_empty_no_section(self):
        cfg = Config(
            llm=LLMConfig(api_key="k", model="m"),
            skills=SkillsConfig(enabled=[], cli_dir=None),
        )
        prompt = build_system_prompt(
            time_context="2025-01-01 12:00",
            config=cfg,
            has_web_extractor=False,
            mode="full",
        )
        assert "可用技能" not in prompt or "（索引）" not in prompt

    def test_skills_index_includes_load_skill_hint(self, tmp_path):
        ex = tmp_path / "example"
        ex.mkdir()
        (ex / "SKILL.md").write_text(
            "---\nname: Example\ndescription: test\n---\n\nbody\n",
            encoding="utf-8",
        )
        cfg = Config(
            llm=LLMConfig(api_key="k", model="m"),
            skills=SkillsConfig(enabled=["example"], cli_dir=str(tmp_path)),
        )
        prompt = build_system_prompt(
            time_context="2025-01-01 12:00",
            config=cfg,
            has_web_extractor=False,
            mode="full",
        )
        assert "load_skill" in prompt
        assert "Available Skills" in prompt or "Index" in prompt
