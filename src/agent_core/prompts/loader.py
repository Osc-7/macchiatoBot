"""
Prompt 加载与组合

参考 [OpenClaw 系统提示词](https://docs.openclaw.ai/zh-CN/concepts/system-prompt) 架构：
- 设计紧凑：人设与时间、Safety、Tooling、渠道 overlay、Workspace 引导、条件 Runtime 扩展分段组合
- 渠道配方见 ``PromptRecipe`` / ``get_recipe``；工作区段按 ``workspace_sections`` 顺序注入

组装顺序（``mode=full``，默认配方）：
1. Identity / Soul — 人设与基调（可按渠道省略）
2. Runtime: time — **静态**说明（实时时刻在用户消息 ``[Time:...]`` 前缀）
3. Safety — runtime_safety
4. Tooling — tools_kernel
5. Channel overlay — 可选（如 ``shuiyuan/system``）
6. Workspace — 按配方注入 ``agents`` / ``multi_agent`` / ``schedule`` / ``user`` 等；可选 Skills 索引（随本机 ``user.md`` / CLI skills 变化）
7. Runtime 扩展 — 联网、抓取、文件、记忆说明（按配置）

``build_agent_system_prompt`` 还会在末尾追加「记忆上下文」「自动化摘要」（按会话变化），利于前缀缓存静态 instruction 段。

minimal（后台 Core / background）：静态 time 说明 → safety → tools → runtime 扩展；无人设、无 overlay、无 Workspace。

Skills 采用渐进式披露：system prompt 仅注入 metadata（name + description），
完整内容需通过 load_skill 工具按需加载。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Tuple

import yaml

from agent_core.config import Config
from agent_core.prompts.skills_roots import (
    format_skills_index_lines,
    list_skills_in_roots,
    merge_skill_roots,
    resolve_skill_md_path,
)

PromptMode = Literal["full", "minimal", "none"]
"""系统提示组装模式：

- full: 主会话与子 Agent（``CoreProfile.mode`` 为 full/sub），identity/soul → time → safety → tools → Workspace 引导 → runtime 扩展
- minimal: 后台 background Core，time → safety → tools → runtime 扩展（无人设块与 Workspace 引导）
- none: 仅 Identity（基本身份）
"""

DEFAULT_MAX_SECTION_CHARS = 8000
"""单 section 默认最大字符数，超出则截断并加标记"""

TRUNCATION_MARKER = "\n\n<!-- 内容过长，已截断 -->"
"""大文件截断后的标记"""


@dataclass(frozen=True)
class PromptRecipe:
    """按来源组装的 system prompt 配方（声明式，避免散落 if source==）。"""

    identity: str | None = "system/identity"
    soul: str | None = "system/soul"
    channel_overlay: str | None = None
    workspace_sections: tuple[str, ...] = ("agents", "multi_agent", "schedule", "user")
    include_skills: bool = True
    include_digest: bool = True


_RECIPES: dict[str, PromptRecipe] = {
    "shuiyuan": PromptRecipe(
        identity=None,
        soul=None,
        channel_overlay="shuiyuan/system",
        workspace_sections=("multi_agent",),
        include_skills=True,
        include_digest=False,
    ),
}


def get_recipe(source: str) -> PromptRecipe:
    """按 ``AgentCore._source`` 解析配方；未知来源使用默认（飞书/cli 等价）。"""
    return _RECIPES.get((source or "").strip(), PromptRecipe())


def _get_prompts_dir() -> Path:
    """获取 prompts 包根目录"""
    return Path(__file__).resolve().parent


def _resolve_cli_dir(cli_dir: Optional[str]) -> Optional[Path]:
    """解析 cli_dir 配置为 Path，展开 ~。若为空或目录不存在则返回 None。"""
    if not cli_dir or not str(cli_dir).strip():
        return None
    p = Path(cli_dir.strip()).expanduser().resolve()
    return p if p.is_dir() else None


def resolve_skills_cli_path(
    config: Config,
    *,
    source: str,
    user_id: str,
    profile: Optional[Any] = None,
    bash_workspace_admin: Optional[bool] = None,
) -> Optional[Path]:
    """
    当前 Core 使用的 **主** Skills CLI 根目录（``.agents/skills``）。

    与 bash / write_file / ``session_paths`` 一致：隔离模式下即会话 ``~/.agents/skills``。
    完整扫描请用 ``resolve_skills_roots``（还会包含 ``.macchiato/skills``）。
    """
    roots = resolve_skills_roots(
        config,
        source=source,
        user_id=user_id,
        profile=profile,
        bash_workspace_admin=bash_workspace_admin,
    )
    for root in roots:
        if root.name == "skills" and root.parent.name == ".agents":
            return root
    return roots[-1] if roots else None


def resolve_skills_roots(
    config: Config,
    *,
    source: str,
    user_id: str,
    profile: Optional[Any] = None,
    bash_workspace_admin: Optional[bool] = None,
) -> list[Path]:
    """
    有序技能根目录：``.macchiato/skills`` → ``.agents/skills``（同名前者优先）。

    非隔离且 session home 即进程 ``Path.home()`` 时，``.agents`` 根使用配置
    ``skills.cli_dir``（默认 ``~/.agents/skills``）。
    """
    from agent_core.agent.session_paths import session_home_path

    default_cli = _resolve_cli_dir(getattr(config.skills, "cli_dir", None))
    home = session_home_path(
        config,
        source=source,
        user_id=user_id,
        profile=profile,
        bash_workspace_admin=bash_workspace_admin,
    )
    prefer_default = home.resolve() == Path.home().resolve() and default_cli is not None
    return merge_skill_roots(
        home=home,
        default_cli=default_cli,
        prefer_default_cli_as_agents=prefer_default,
    )


def _resolve_skill_path(
    skill_name: str,
    cli_dir_path: Optional[Path] = None,
    *,
    skill_roots: Optional[list[Path]] = None,
) -> Optional[Path]:
    """解析技能 SKILL.md：优先 ``skill_roots``，否则单根 ``cli_dir_path``。"""
    if skill_roots:
        return resolve_skill_md_path(skill_name, skill_roots)
    if cli_dir_path:
        return resolve_skill_md_path(skill_name, [cli_dir_path])
    return None


def _list_cli_dir_skills(cli_dir_path: Path) -> list[str]:
    """列出 cli_dir 下所有含 SKILL.md 的子目录名。"""
    return list_skills_in_roots([cli_dir_path])


def _load_section(
    name: str,
    max_chars: int = DEFAULT_MAX_SECTION_CHARS,
) -> str:
    """
    加载 prompts/system/{name}.md 片段。
    空文件或仅空白内容返回空字符串。超出 max_chars 时截断并追加 TRUNCATION_MARKER。
    """
    path = _get_prompts_dir() / "system" / f"{name}.md"
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return ""
    if len(content) > max_chars:
        content = content[:max_chars].rstrip() + TRUNCATION_MARKER
    return content


def _load_relative_md(
    relative_stem: str,
    max_chars: int = DEFAULT_MAX_SECTION_CHARS,
) -> str:
    """
    加载 prompts 目录下任意 ``{relative_stem}.md``（如 ``system/identity``、``shuiyuan/system``）。
    """
    path = _get_prompts_dir() / f"{relative_stem}.md"
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return ""
    if len(content) > max_chars:
        content = content[:max_chars].rstrip() + TRUNCATION_MARKER
    return content


def _parse_skill_frontmatter(content: str) -> Tuple[Optional[str], Optional[str]]:
    """
    解析 SKILL.md 的 YAML frontmatter，提取 name 与 description。
    返回 (display_name, description)，未找到时返回 (None, None)。
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return None, None
    try:
        meta = yaml.safe_load(match.group(1))
        if not meta or not isinstance(meta, dict):
            return None, None
        name = meta.get("name")
        desc = meta.get("description")
        return (
            str(name).strip() if name else None,
            str(desc).strip() if desc else None,
        )
    except Exception:
        return None, None


def _load_skill_metadata(
    skill_name: str,
    cli_dir_path: Optional[Path] = None,
    *,
    skill_roots: Optional[list[Path]] = None,
) -> Optional[str]:
    """
    加载技能 metadata：仅解析 frontmatter 的 name 和 description。
    返回格式：'- **{display_name}** (`{skill_name}`): {description}'
    若解析失败则用 skill_name 作为显示名。
    """
    path = _resolve_skill_path(skill_name, cli_dir_path, skill_roots=skill_roots)
    if not path:
        return None
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return None
    display_name, description = _parse_skill_frontmatter(content)
    display_name = display_name or skill_name
    description = description or "(no description)"
    return f"- **{display_name}** (`{skill_name}`): {description}"


def _format_skills_index(
    enabled: list[str],
    cli_dir_path: Optional[Path] = None,
    *,
    skill_roots: Optional[list[Path]] = None,
    source_note: str = "",
) -> str:
    """
    构建技能索引（渐进式披露第一层）。

    优先扫描 ``skill_roots``（``.macchiato/skills`` → ``.agents/skills``）；
    否则回退单根 ``cli_dir_path``。``enabled`` 为空则展示全部，非空则仅展示 enabled。
    """
    roots = list(skill_roots or [])
    if not roots and cli_dir_path is not None:
        roots = [cli_dir_path]
    if not roots:
        return ""
    seen: set[str] = set()
    lines: list[str] = []
    all_skills = list_skills_in_roots(roots)
    to_show = enabled if enabled else all_skills
    for skill_name in to_show:
        if skill_name in seen or skill_name not in all_skills:
            continue
        seen.add(skill_name)
        line = _load_skill_metadata(skill_name, skill_roots=roots)
        if line:
            lines.append(line)
    note = (source_note or "").strip() or (
        "Skills are listed from `.macchiato/skills` then `.agents/skills` "
        "(same-name: `.macchiato` wins). `npx skills add -g` installs into `.agents/skills`."
    )
    return format_skills_index_lines(lines, source_note=note)


def load_skill_content(
    skill_name: str,
    max_chars: int = DEFAULT_MAX_SECTION_CHARS,
    cli_dir_path: Optional[Path] = None,
    *,
    skill_roots: Optional[list[Path]] = None,
) -> str:
    """
    加载技能完整内容（供 load_skill 工具调用）。

    从 ``skill_roots`` 或 ``cli_dir_path`` 读取 ``{name}/SKILL.md``。
    超出 max_chars 时截断。
    """
    path = _resolve_skill_path(skill_name, cli_dir_path, skill_roots=skill_roots)
    if not path:
        return ""
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return ""
    if len(content) > max_chars:
        content = content[:max_chars].rstrip() + TRUNCATION_MARKER
    return content


def _maybe_append(parts: list, content: str) -> None:
    """非空 content 则追加到 parts"""
    if content and content.strip():
        parts.append(content.strip())


def _load_user_section(max_chars: int = DEFAULT_MAX_SECTION_CHARS) -> str:
    """加载 USER。优先 user.md，不存在时回退 user.example.md。"""
    system_dir = _get_prompts_dir() / "system"
    path = system_dir / "user.md"
    if not path.exists():
        path = system_dir / "user.example.md"
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return ""
    if len(content) > max_chars:
        content = content[:max_chars].rstrip() + TRUNCATION_MARKER
    return content


def build_system_prompt(
    config: Config,
    has_web_extractor: bool,
    has_file_tools: bool = False,
    mode: PromptMode = "full",
    max_section_chars: int = DEFAULT_MAX_SECTION_CHARS,
    recipe: Optional[PromptRecipe] = None,
    skills_cli_path: Optional[Path] = None,
    skills_roots: Optional[list[Path]] = None,
    skills_index_override: Optional[str] = None,
) -> str:
    """
    构建 Agent 系统提示。先人设与静态时间说明（实时时刻在用户消息前缀）、安全边界，
    再工具长文，渠道 overlay 与 Workspace 由 ``recipe`` 决定。

    ``skills_index_override``：远程工作区激活时注入已扫描的远程技能索引，跳过本机扫描。
    """
    rec = recipe if recipe is not None else PromptRecipe()
    parts: list[str] = []

    def load(name: str) -> str:
        """按给定 name 加载 system section，封装 _load_section 以便复用。"""
        return _load_section(name, max_section_chars)

    if mode == "none":
        stem = rec.identity or "identity"
        _maybe_append(parts, _load_relative_md(stem, max_section_chars))
        return "\n\n".join(parts)

    # ---------- 1. Identity / Soul（仅 full；minimal 从时间段起）----------
    if mode == "full":
        if rec.identity:
            _maybe_append(parts, _load_relative_md(rec.identity, max_section_chars))
        if rec.soul:
            _maybe_append(parts, _load_relative_md(rec.soul, max_section_chars))

    # ---------- 2. Runtime: 时间说明（静态；实时时刻在用户消息前缀 [Time:...]，便于缓存）----------
    if mode in ("full", "minimal"):
        time_section = load("runtime_time")
        if time_section:
            _maybe_append(parts, time_section.strip())

    # ---------- 3. Safety ----------
    if mode in ("full", "minimal"):
        _maybe_append(parts, load("runtime_safety"))

    # ---------- 4. Tooling ----------
    if mode in ("full", "minimal"):
        _maybe_append(parts, load("tools_kernel"))

    # ---------- 5. 渠道 overlay（仅 full）----------
    if mode == "full" and rec.channel_overlay:
        _maybe_append(parts, _load_relative_md(rec.channel_overlay, max_section_chars))

    # ---------- 6. Workspace + Skills（仅 full）----------
    if mode == "full":
        need_workspace_block = bool(rec.workspace_sections) or rec.include_skills
        if need_workspace_block:
            parts.append(
                "---\n# Workspace Files (injected)\n"
                "以下为行为规范、日程与用户偏好等引导文件（identity / soul 已置于上文），已注入。\n---"
            )
            for section in rec.workspace_sections:
                if section == "user":
                    user_content = _load_user_section(max_section_chars)
                    if user_content:
                        parts.append(user_content)
                else:
                    _maybe_append(parts, load(section))
        if rec.include_skills:
            # None = scan local roots; "" = explicitly empty (e.g. remote with no skills).
            if skills_index_override is not None:
                skills_index = (skills_index_override or "").strip()
            else:
                roots = list(skills_roots or [])
                if not roots and skills_cli_path is not None:
                    roots = [skills_cli_path]
                if not roots:
                    fallback = _resolve_cli_dir(getattr(config.skills, "cli_dir", None))
                    if fallback is not None:
                        roots = [fallback]
                skills_index = _format_skills_index(
                    config.skills.enabled or [],
                    skill_roots=roots,
                )
            if skills_index:
                if not need_workspace_block:
                    parts.append(
                        "---\n# Workspace Files (injected)\n"
                        "以下为技能索引等引导内容，已注入。\n---"
                    )
                _maybe_append(parts, skills_index)

    # ---------- 7. Runtime 扩展（联网 / 抓取 / 文件 / 记忆）；不含 runtime_time ----------
    if mode in ("full", "minimal"):
        if config.mcp.enabled:
            web_capabilities = [
                "- 当前新闻、热点事件",
                "- 实时天气信息",
                "- 股票价格、汇率等金融数据",
                "- 最新技术资讯、行业动态",
                "- 其他需要实时更新的信息",
            ]
            web_search = load("runtime_web_search")
            if web_search:
                _maybe_append(
                    parts, web_search.format(capabilities="\n".join(web_capabilities))
                )
        if has_web_extractor:
            _maybe_append(parts, load("runtime_web_extractor"))
        if has_file_tools:
            _maybe_append(parts, load("runtime_file_tools"))
        if config.memory.enabled:
            _maybe_append(parts, load("runtime_memory"))
        if mode == "full":
            _maybe_append(parts, load("runtime_goals"))

    return "\n\n".join(parts)
