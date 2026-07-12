"""
按需加载技能完整内容（渐进式披露第二层）。

与 loader 配合：system prompt 仅注入 skill 的 metadata，
Agent 在需要时调用此工具获取完整 SKILL 说明。

技能目录与当前会话一致：
- 本地：``.macchiato/skills`` → ``.agents/skills``（同名前者优先；
  ``npx skills add -g`` 安装到后者）
- 远程：读取当前远程工作区同样两处路径，而不是 daemon 本机技能目录
"""

from agent_core.agent.memory_paths import effective_memory_namespace_from_execution_context
from agent_core.config import Config
from agent_core.prompts.loader import (
    load_skill_content,
    resolve_skills_roots,
)

from agent_core.tools.base import BaseTool, ToolDefinition, ToolParameter, ToolResult

_SOFT_REMOTE_ERRORS = {
    "",
    "invalid remote skill path",
    "skill_name cannot be empty",
}


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
                "Call when the skills index is insufficient to complete the task. "
                "Looks up `.macchiato/skills` then `.agents/skills` in the current "
                "workspace (remote when remote mode is active)."
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
                "读取当前工作区技能目录下的 SKILL.md："
                "`.macchiato/skills` 优先，其次 `.agents/skills`"
                "（隔离模式下与 shell 中 ~/.agents/skills 为同一树；"
                "远程模式下读远程工作区）。"
                f" 技能名：{skill_list}。",
            ],
            tags=["skill", "load", "progressive-disclosure"],
        )

    async def _load_remote_skill_content(
        self,
        *,
        skill_name: str,
        exec_ctx: dict,
    ) -> tuple[str, str | None, dict]:
        session_id = str(exec_ctx.get("session_id") or "").strip()
        if not session_id:
            return "", None, {}

        try:
            from agent_core.remote.skills_index import load_remote_skill_markdown
            from agent_core.remote.workspace_state import get_remote_workspace_state
        except Exception:
            return "", None, {}

        remote_state = get_remote_workspace_state(session_id)
        if remote_state is None:
            return "", None, {}

        return await load_remote_skill_markdown(
            session_id=session_id,
            login=remote_state.login,
            skill_name=skill_name,
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
        remote_content, remote_error, remote_metadata = await self._load_remote_skill_content(
            skill_name=skill_name,
            exec_ctx=ctx,
        )

        if remote_metadata.get("workspace_backend") == "remote":
            if remote_content:
                return ToolResult(
                    success=True,
                    data={"skill_name": skill_name, "content": remote_content},
                    message=f"Loaded skill `{skill_name}`.\n\n---\n{remote_content}",
                    metadata=remote_metadata,
                )
            err = (remote_error or "").strip()
            if err and err not in _SOFT_REMOTE_ERRORS:
                return ToolResult(
                    success=False,
                    error="REMOTE_SKILL_READ_FAILED",
                    message=f"读取远程 SKILL.md 失败: {err}",
                    metadata=remote_metadata,
                )
            return ToolResult(
                success=False,
                error="SKILL_NOT_FOUND",
                message=(
                    f"SKILL.md not found for '{skill_name}' in remote workspace "
                    "(.macchiato/skills or .agents/skills)"
                ),
                metadata=remote_metadata,
            )

        src, uid = effective_memory_namespace_from_execution_context(ctx)
        roots = resolve_skills_roots(
            self._config,
            source=src,
            user_id=uid,
            profile=None,
            bash_workspace_admin=ctx.get("bash_workspace_admin"),
        )
        content = load_skill_content(skill_name, skill_roots=roots)
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
