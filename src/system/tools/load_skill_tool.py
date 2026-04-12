"""
按需加载技能完整内容（渐进式披露第二层）。

与 loader 配合：system prompt 仅注入 skill 的 metadata，
Agent 在需要时调用此工具获取完整 SKILL 说明。

技能目录与当前会话一致：工作区隔离时为 ``{workspace}/{frontend}/{user}/.agents/skills``，
与 bash / write_file 下的 ``~/.agents/skills`` 为同一树；非隔离或工作区管理员时为配置项 ``skills.cli_dir``（默认进程 ``~/.agents/skills``）。
"""

from agent_core.agent.memory_paths import effective_memory_namespace_from_execution_context
from agent_core.config import Config
from agent_core.prompts.loader import load_skill_content, resolve_skills_cli_path

from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult


class LoadSkillTool(BaseTool):
    """按需加载已启用技能的完整 SKILL.md 内容。"""

    def __init__(self, config: Config):
        self._config = config

    @property
    def name(self) -> str:
        return "load_skill"

    def _usage_skills_hint(self) -> str:
        """工具定义无执行上下文，仅能用配置中的 enabled 作名称提示。"""
        en = self._config.skills.enabled or []
        if en:
            return ", ".join(f"`{s}`" for s in en)
        return "以系统提示 **Available Skills** 索引中的名称为准"

    def get_definition(self) -> ToolDefinition:
        skill_list = self._usage_skills_hint()
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
                "读取本会话技能目录下的 SKILL.md（隔离模式下与 shell 中 ~/.agents/skills 为同一目录）。"
                f" 技能名：{skill_list}。",
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

        ctx = kwargs.get("__execution_context__") or {}
        src, uid = effective_memory_namespace_from_execution_context(ctx)
        cli_path = resolve_skills_cli_path(
            self._config,
            source=src,
            user_id=uid,
            profile=None,
            bash_workspace_admin=ctx.get("bash_workspace_admin"),
        )
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
