"""
按需加载技能完整内容（渐进式披露第二层）。

与 loader 配合：system prompt 仅注入 skill 的 metadata，
Agent 在需要时调用此工具获取完整 SKILL 说明。
技能仅从 cli_dir（~/.agents/skills）读取。
"""

from schedule_agent.config import Config
from schedule_agent.prompts.loader import (
    _list_cli_dir_skills,
    _resolve_cli_dir,
    load_skill_content,
)

from .base import BaseTool, ToolDefinition, ToolParameter, ToolResult


class LoadSkillTool(BaseTool):
    """按需加载已启用技能的完整 SKILL.md 内容。"""

    def __init__(self, config: Config):
        self._config = config

    @property
    def name(self) -> str:
        return "load_skill"

    def _get_all_available_skills(self) -> list[str]:
        """从 cli_dir 列出技能；enabled 非空时仅返回 enabled 中且存在的。"""
        cli_path = _resolve_cli_dir(getattr(self._config.skills, "cli_dir", None))
        if not cli_path:
            return []
        all_skills = set(_list_cli_dir_skills(cli_path))
        enabled = self._config.skills.enabled or []
        return [n for n in (enabled if enabled else all_skills) if n in all_skills]

    def get_definition(self) -> ToolDefinition:
        all_skills = self._get_all_available_skills()
        skill_list = ", ".join(f"`{s}`" for s in all_skills) if all_skills else "(none)"
        return ToolDefinition(
            name=self.name,
            description=(
                "Load full SKILL.md content for an enabled skill. "
                "Call when the skills index is insufficient to complete the task."
            ),
            parameters=[
                ToolParameter(
                    name="skill_name",
                    type="string",
                    description="Skill name from the skills index, e.g. my-skill",
                    required=True,
                ),
            ],
            examples=[
                {
                    "description": "Load full docs for my-skill",
                    "params": {"skill_name": "my-skill"},
                },
            ],
            usage_notes=[
                f"Available skills (from ~/.agents/skills): {skill_list}.",
            ],
            tags=["skill", "load", "progressive-disclosure"],
        )

    async def execute(self, **kwargs) -> ToolResult:
        skill_name = (kwargs.get("skill_name") or "").strip()
        if not skill_name:
            return ToolResult(
                success=False,
                error="INVALID_ARGUMENTS",
                message="skill_name cannot be empty",
            )

        cli_path = _resolve_cli_dir(getattr(self._config.skills, "cli_dir", None))
        content = load_skill_content(skill_name, cli_dir_path=cli_path)
        if not content:
            return ToolResult(
                success=False,
                error="SKILL_NOT_FOUND",
                message=f"SKILL.md not found for '{skill_name}'",
            )

        return ToolResult(
            success=True,
            data={"skill_name": skill_name, "content": content},
            message=f"Loaded skill `{skill_name}`.\n\n---\n{content}",
        )
