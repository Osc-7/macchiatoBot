"""
Prompt 加载与组合

参考 [OpenClaw 系统提示词](https://docs.openclaw.ai/zh-CN/concepts/system-prompt) 架构：
- 设计紧凑，使用固定部分（Tooling、Safety、Skills、Runtime）
- 工作区引导文件在「Workspace Files (injected)」下按顺序注入

固定部分顺序：
1. Tooling — 工具列表与使用说明
2. Safety — 简短防护提醒
3. Workspace Files (injected) — 以下为引导文件
4. 引导文件：identity → soul → agents → schedule → user → skills(可选)
5. Runtime — 当前时间、联网/文件/记忆等

Skills 采用渐进式披露：system prompt 仅注入 metadata（name + description），
完整内容需通过 load_skill 工具按需加载。
"""

import re
from pathlib import Path
from typing import Any, Literal, Optional, Tuple

import yaml

from agent_core.config import Config

PromptMode = Literal["full", "minimal", "none"]
"""系统提示组装模式：

- full: 主 Agent，包含全部固定部分 + 引导文件
- minimal: 子 Agent，仅 Tooling + Safety + Runtime
- none: 仅 Identity（基本身份）
"""

DEFAULT_MAX_SECTION_CHARS = 8000
"""单 section 默认最大字符数，超出则截断并加标记"""

TRUNCATION_MARKER = "\n\n<!-- 内容过长，已截断 -->"
"""大文件截断后的标记"""


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
    当前 Core 使用的 Skills CLI 根目录（各 skill 子目录下含 SKILL.md）。

    与 bash / write_file / ``session_paths`` 一致：隔离模式下即会话 ``~/.agents/skills`` 实体路径。
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
    if home.resolve() == Path.home().resolve() and default_cli is not None:
        return default_cli

    p = (home / ".agents" / "skills").resolve()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        if not p.is_dir():
            return None
    return p if p.is_dir() else None


def _resolve_skill_path(
    skill_name: str, cli_dir_path: Optional[Path] = None
) -> Optional[Path]:
    """解析技能 SKILL.md 路径。仅从 cli_dir 读取（~/.agents/skills）。"""
    if cli_dir_path:
        cand = cli_dir_path / skill_name / "SKILL.md"
        if cand.exists():
            return cand
    return None


def _list_cli_dir_skills(cli_dir_path: Path) -> list[str]:
    """列出 cli_dir 下所有含 SKILL.md 的子目录名。"""
    names: list[str] = []
    if not cli_dir_path.is_dir():
        return names
    for d in cli_dir_path.iterdir():
        if d.is_dir() and (d / "SKILL.md").exists():
            names.append(d.name)
    return sorted(names)


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
    skill_name: str, cli_dir_path: Optional[Path] = None
) -> Optional[str]:
    """
    加载技能 metadata：仅解析 frontmatter 的 name 和 description。
    返回格式：'- **{display_name}** (`{skill_name}`): {description}'
    若解析失败则用 skill_name 作为显示名。
    """
    path = _resolve_skill_path(skill_name, cli_dir_path)
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
) -> str:
    """
    构建技能索引（渐进式披露第一层）。
    从 cli_dir（~/.agents/skills）读取；enabled 为空则展示全部，非空则仅展示 enabled 中的。
    """
    seen: set[str] = set()
    lines: list[str] = []
    if not cli_dir_path:
        return ""
    all_skills = _list_cli_dir_skills(cli_dir_path)
    to_show = enabled if enabled else all_skills
    for skill_name in to_show:
        if skill_name in seen or skill_name not in all_skills:
            continue
        seen.add(skill_name)
        line = _load_skill_metadata(skill_name, cli_dir_path)
        if line:
            lines.append(line)
    if not lines:
        return ""
    index = "\n".join(lines)
    return (
        "## Available Skills (Index)\n\n"
        "**Progressive disclosure**: Only names and brief descriptions are shown here to save context. "
        "When a task requires a skill, call `load_skill(skill_name)` to load the full SKILL content, then follow its instructions.\n\n"
        f"{index}\n\n"
        "> Call `load_skill(skill_name)` to fetch full skill documentation when needed."
    )


def load_skill_content(
    skill_name: str,
    max_chars: int = DEFAULT_MAX_SECTION_CHARS,
    cli_dir_path: Optional[Path] = None,
) -> str:
    """
    加载技能完整内容（供 load_skill 工具调用）。

    仅从 CLI skills 目录读取（例如 ~/.agents/skills/{skill_name}/SKILL.md），
    不再从仓库内 prompts/skills/ 读取。超出 max_chars 时截断。
    """
    path = _resolve_skill_path(skill_name, cli_dir_path)
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
    time_context: str,
    config: Config,
    has_web_extractor: bool,
    has_file_tools: bool = False,
    mode: PromptMode = "full",
    max_section_chars: int = DEFAULT_MAX_SECTION_CHARS,
    sub_content_path: str | None = None,
    skills_cli_path: Optional[Path] = None,
) -> str:
    """
    构建 Agent 系统提示。按 OpenClaw 风格固定部分 + 工作区引导注入顺序组装。
    """
    parts: list[str] = []

    def load(name: str) -> str:
        """按给定 name 加载 system section，封装 _load_section 以便复用。"""
        return _load_section(name, max_section_chars)

    # ---------- 1. Tooling（工具列表与使用说明）----------
    if mode in ("full", "minimal"):
        _maybe_append(parts, load("tools_kernel"))

    # ---------- 2. Safety（简短防护）----------
    if mode in ("full", "minimal"):
        _maybe_append(parts, load("runtime_safety"))

    if mode == "none":
        _maybe_append(parts, load("identity"))
        return "\n\n".join(parts)

    # ---------- 3. Workspace Files (injected) — 以下为引导文件 ----------
    if mode == "full":
        parts.append(
            "---\n# Workspace Files (injected)\n以下为工作区引导文件，已注入。\n---"
        )

    # ---------- 4. 引导文件顺序：IDENTITY → SOUL → AGENTS → SCHEDULE → USER → SKILLS ----------
    if mode == "full":
        _maybe_append(parts, load("identity"))
        _maybe_append(parts, load("soul"))
        _maybe_append(parts, load("agents"))
        _maybe_append(parts, load("schedule"))
        user_content = _load_user_section(max_section_chars)
        if user_content:
            parts.append(user_content)
        cli_path = (
            skills_cli_path
            if skills_cli_path is not None
            else _resolve_cli_dir(getattr(config.skills, "cli_dir", None))
        )
        skills_index = _format_skills_index(config.skills.enabled or [], cli_path)
        if skills_index:
            _maybe_append(parts, skills_index)

    # ---------- 5. Runtime（当前时间、联网/文件/记忆）----------
    if mode in ("full", "minimal"):
        time_section = load("runtime_time")
        if time_section:
            _maybe_append(parts, time_section.format(time_context=time_context))
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

    return "\n\n".join(parts)


def build_shuiyuan_system_prompt(
    time_context: str,
    config: Config,
    memory_dir: str = "./data/memory/long_term/shuiyuan",
    recent_topics: Optional[list] = None,
) -> str:
    """
    构建水源社区 Agent 的系统提示。

    使用 prompts/shuiyuan/system.md + 水源 MEMORY.md + 最近话题 + 时间上下文，
    与主 Agent 隔离。
    """
    parts: list[str] = []
    shuiyuan_prompt_dir = _get_prompts_dir() / "shuiyuan"
    shuiyuan_system = (
        (shuiyuan_prompt_dir / "system.md").read_text(encoding="utf-8").strip()
    )
    parts.append(shuiyuan_system)

    memory_path = Path(memory_dir) / "MEMORY.md"
    if memory_path.exists():
        memory_content = memory_path.read_text(encoding="utf-8").strip()
        if memory_content:
            parts.append("---\n## 水源社区长期记忆 (MEMORY.md)\n\n" + memory_content)

    if recent_topics:
        topic_lines = []
        for t in recent_topics:
            ts = (getattr(t, "created_at", "") or "")[:10]
            content = getattr(t, "content", "") or ""
            prefix = f"[{ts}] " if ts else ""
            topic_lines.append(f"- {prefix}{content}")
        parts.append("---\n## 与该用户的最近话题\n\n" + "\n".join(topic_lines))

    parts.append("---\n## 当前时间\n\n" + time_context)

    return "\n\n".join(parts)
