"""
LoadSkillTool 与 loader 渐进式披露相关测试
"""

from pathlib import Path

import pytest

from agent_core.config import CommandToolsConfig, Config, LLMConfig, SkillsConfig
from agent_core.prompts.loader import (
    _format_skills_index,
    _parse_skill_frontmatter,
    build_system_prompt,
    load_skill_content,
)
from agent_core.remote.workspace_state import (
    activate_remote_workspace,
    clear_remote_workspace_state,
)
from macchiato_remote.protocol import RemoteFileReadResult
from system.tools.load_skill_tool import LoadSkillTool


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

    @pytest.mark.asyncio
    async def test_execute_loads_from_linux_home_when_os_user_isolated(self, tmp_path):
        """Linux 用户隔离下 load_skill 与 bash 的 HOME 一致，不回落到 legacy workspace。"""
        homes = tmp_path / "homes"
        linux_skills = homes / "m_feishu_u9" / ".agents" / "skills" / "example"
        linux_skills.mkdir(parents=True)
        (linux_skills / "SKILL.md").write_text(
            "---\nname: 示例技能\ndescription: d\n---\n\nLinux home 版本\n",
            encoding="utf-8",
        )
        legacy_skills = (
            tmp_path
            / "workspace_parent"
            / "feishu"
            / "u9"
            / ".agents"
            / "skills"
            / "example"
        )
        legacy_skills.mkdir(parents=True)
        (legacy_skills / "SKILL.md").write_text(
            "---\nname: 示例技能\ndescription: d\n---\n\nlegacy workspace 版本\n",
            encoding="utf-8",
        )
        cfg = Config(
            llm=LLMConfig(api_key="k", model="m"),
            skills=SkillsConfig(enabled=["example"]),
            command_tools=CommandToolsConfig(
                base_dir=str(tmp_path),
                workspace_base_dir=str(tmp_path / "workspace_parent"),
                workspace_isolation_enabled=True,
                bash_os_user_enabled=True,
                bash_os_user_home_base_dir=str(homes),
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
        assert "Linux home 版本" in r.message
        assert "legacy workspace 版本" not in r.message

    @pytest.mark.asyncio
    async def test_execute_admin_loads_from_own_logic_linux_home(self, tmp_path):
        """管理员 Core 仍读取自己的逻辑 Linux home，不切到共享 admin home。"""
        homes = tmp_path / "homes"
        logic_skills = homes / "m_feishu_u9" / ".agents" / "skills" / "example"
        logic_skills.mkdir(parents=True)
        (logic_skills / "SKILL.md").write_text(
            "---\nname: 示例技能\ndescription: d\n---\n\nadmin logic home\n",
            encoding="utf-8",
        )
        shared_admin_skills = homes / "mac_admin" / ".agents" / "skills" / "example"
        shared_admin_skills.mkdir(parents=True)
        (shared_admin_skills / "SKILL.md").write_text(
            "---\nname: 示例技能\ndescription: d\n---\n\nshared admin home\n",
            encoding="utf-8",
        )
        cfg = Config(
            llm=LLMConfig(api_key="k", model="m"),
            skills=SkillsConfig(enabled=["example"]),
            command_tools=CommandToolsConfig(
                base_dir=str(tmp_path),
                workspace_base_dir=str(tmp_path / "workspace_parent"),
                workspace_isolation_enabled=True,
                workspace_admin_memory_owners=["feishu:u9"],
                bash_os_user_enabled=True,
                bash_os_user_home_base_dir=str(homes),
                bash_os_admin_system_users={"feishu:u9": "mac_admin"},
            ),
        )
        tool = LoadSkillTool(config=cfg)
        r = await tool.execute(
            skill_name="example",
            __execution_context__={
                "source": "feishu",
                "user_id": "u9",
                "bash_workspace_admin": True,
            },
        )
        assert r.success is True
        assert "admin logic home" in r.message
        assert "shared admin home" not in r.message

    @pytest.mark.asyncio
    async def test_execute_loads_from_remote_workspace_when_active(
        self, tmp_path, monkeypatch
    ):
        """远程工作区下 load_skill 读取远程 ~/.agents/skills，而不是 daemon 本地路径。"""
        pytest.importorskip("agent_core.remote.pathmap")
        pytest.importorskip("agent_core.remote.worker_registry")

        class FakeRemoteRegistry:
            def __init__(self):
                self.calls = []

            async def file_read(self, **kwargs):
                self.calls.append(kwargs)
                return RemoteFileReadResult(
                    request_id="r1",
                    path=kwargs["path"],
                    content="---\nname: 远程技能\ndescription: d\n---\n\nremote body",
                    encoding="utf-8",
                )

        fake = FakeRemoteRegistry()
        monkeypatch.setattr(
            "agent_core.remote.worker_registry.get_remote_worker_registry",
            lambda: fake,
        )
        clear_remote_workspace_state()
        try:
            activate_remote_workspace(
                session_id="feishu:u9",
                login="local-dev",
                requested_path="~/proj",
                resolved_path=str(tmp_path / "remote-proj"),
            )
            cfg = Config(
                llm=LLMConfig(api_key="k", model="m"),
                skills=SkillsConfig(enabled=["example"]),
                command_tools=CommandToolsConfig(
                    base_dir=str(tmp_path),
                    workspace_base_dir=str(tmp_path / "workspace_parent"),
                    workspace_isolation_enabled=True,
                ),
            )
            tool = LoadSkillTool(config=cfg)
            r = await tool.execute(
                skill_name="example",
                __execution_context__={
                    "source": "feishu",
                    "user_id": "u9",
                    "session_id": "feishu:u9",
                    "bash_workspace_admin": False,
                },
            )
        finally:
            clear_remote_workspace_state()

        assert r.success is True
        assert "remote body" in r.message
        assert fake.calls[0]["login"] == "local-dev"
        assert fake.calls[0]["session_id"] == "feishu:u9"
        assert fake.calls[0]["path"] == ".agents/skills/example/SKILL.md"


class TestBuildSystemPromptSkills:
    def test_skills_empty_no_section(self):
        cfg = Config(
            llm=LLMConfig(api_key="k", model="m"),
            skills=SkillsConfig(enabled=[], cli_dir=None),
        )
        prompt = build_system_prompt(
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
            config=cfg,
            has_web_extractor=False,
            mode="full",
        )
        assert "load_skill" in prompt
        assert "Available Skills" in prompt or "Index" in prompt
