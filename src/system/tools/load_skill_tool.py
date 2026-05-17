"""
按需加载技能完整内容（渐进式披露第二层）。

与 loader 配合：system prompt 仅注入 skill 的 metadata，
Agent 在需要时调用此工具获取完整 SKILL 说明。

技能目录与当前会话一致：工作区隔离时为 ``{workspace}/{frontend}/{user}/.agents/skills``，
与 bash / write_file 下的 ``~/.agents/skills`` 为同一树；非隔离或工作区管理员时为配置项 ``skills.cli_dir``（默认进程 ``~/.agents/skills``）。
"""

from agent_core.agent.memory_paths import effective_memory_namespace_from_execution_context
from agent_core.config import Config
from agent_core.prompts.loader import (
    DEFAULT_MAX_SECTION_CHARS,
    TRUNCATION_MARKER,
    load_skill_content,
    resolve_skills_cli_path,
)

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
            from agent_core.remote.pathmap import normalize_remote_workspace_relative_path
            from agent_core.remote.worker_registry import get_remote_worker_registry
            from agent_core.remote.workspace_state import get_remote_workspace_state
        except Exception:
            return "", None, {}

        remote_state = get_remote_workspace_state(session_id)
        if remote_state is None:
            return "", None, {}

        rel, err = normalize_remote_workspace_relative_path(
            f"~/.agents/skills/{skill_name}/SKILL.md"
        )
        if err or rel is None:
            return "", err or "invalid remote skill path", {
                "workspace_backend": "remote",
                "remote_login": remote_state.login,
            }

        metadata = {
            "workspace_backend": "remote",
            "remote_login": remote_state.login,
            "remote_path": rel,
        }
        try:
            result = await get_remote_worker_registry().file_read(
                login=remote_state.login,
                session_id=session_id,
                path=rel,
                encoding="utf-8",
            )
        except Exception as exc:
            return "", f"远程读取失败: {exc}", metadata

        if result.error:
            if result.error == "FILE_NOT_FOUND":
                return "", None, metadata
            return "", result.error, metadata

        content = (result.content or "").strip()
        if not content:
            return "", None, metadata
        if len(content) > DEFAULT_MAX_SECTION_CHARS:
            content = content[:DEFAULT_MAX_SECTION_CHARS].rstrip() + TRUNCATION_MARKER
        return content, None, metadata

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
        if remote_error:
            return ToolResult(
                success=False,
                error="REMOTE_SKILL_READ_FAILED",
                message=f"读取远程 SKILL.md 失败: {remote_error}",
                metadata=remote_metadata,
            )
        if remote_content:
            return ToolResult(
                success=True,
                data={"skill_name": skill_name, "content": remote_content},
                message=f"Loaded skill `{skill_name}`.\n\n---\n{remote_content}",
                metadata=remote_metadata,
            )

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
