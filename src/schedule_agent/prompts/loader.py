"""
Prompt 加载与组合

基于「文件即配置」架构，参考 OpenClaw 设计：
从 prompts/system/ 加载系统片段，从 prompts/skills/{name}/ 加载技能（SKILL.md）。
支持空文件跳过、大文件截断。

文件角色（full 模式下注入顺序）：
- identity: IDENTITY - 身份档案（名称、形象、签名语等元数据）
- soul: SOUL - 价值观与个性（人格特质、沟通风格、决策原则）
- agents: AGENTS - 代理行为规范（工作流程、行为准则、安全边界）
- skills/schedule: 日程技能 - prompts/skills/schedule/SKILL.md（默认加载）
- user: USER - 用户画像（基本信息、交互偏好、日程偏好、隐私设置）
"""

from pathlib import Path
from typing import Literal

from schedule_agent.config import Config

PromptMode = Literal["full", "minimal", "none"]
"""系统提示组装模式：

- full: 主 Agent，包含全部 sections（默认）
- minimal: 子 Agent，仅 Tooling + Runtime
- none: 基础身份，仅核心身份
"""

DEFAULT_MAX_SECTION_CHARS = 8000
"""单 section 默认最大字符数，超出则截断并加标记"""

TRUNCATION_MARKER = "\n\n<!-- 内容过长，已截断 -->"
"""大文件截断后的标记"""


def _get_prompts_dir() -> Path:
    """获取 prompts 包根目录"""
    return Path(__file__).resolve().parent


def _get_skills_dir() -> Path:
    """获取 prompts/skills 目录路径"""
    return _get_prompts_dir() / "skills"


def _load_section(
    name: str,
    max_chars: int = DEFAULT_MAX_SECTION_CHARS,
) -> str:
    """
    加载 prompts/system/{name}.md 片段。

    空文件或仅空白内容返回空字符串（调用方应跳过）。
    超出 max_chars 时截断并追加 TRUNCATION_MARKER。
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


def _load_skill(
    skill_name: str,
    max_chars: int = DEFAULT_MAX_SECTION_CHARS,
) -> str:
    """
    加载 prompts/skills/{skill_name}/SKILL.md。

    符合 AgentSkills/OpenClaw 规范：每 skill 为独立目录，内含 SKILL.md。
    空文件或仅空白内容返回空字符串。超出 max_chars 时截断。
    """
    path = _get_skills_dir() / skill_name / "SKILL.md"
    if not path.exists():
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
    """
    加载 USER 用户画像。优先加载 user.md（用户本地填写，不提交），
    不存在时回退到 user.example.md（模板，已提交）。保证隐私性。
    """
    system_dir = _get_prompts_dir() / "system"
    # 优先使用 user.md（gitignored，用户本地填写）
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
    tool_mode: str = "full",
) -> str:
    """
    构建 Agent 系统提示。

    按注入顺序组装 sections，空文件自动跳过，大文件截断。

    Args:
        time_context: 当前时间上下文字符串（由 TimeContext.to_prompt_string() 生成）
        config: 应用配置，用于判断是否启用联网搜索等
        has_web_extractor: 是否注册了 extract_web_content 工具
        mode: 组装模式，full | minimal | none
        max_section_chars: 单 section 最大字符数，超出则截断
        tool_mode: 工具暴露模式，full | kernel。kernel 时加载 tools_kernel 片段
    Returns:
        完整的系统提示字符串
    """
    parts: list[str] = []

    def load(name: str) -> str:
        return _load_section(name, max_section_chars)

    # --- 1. Identity 身份档案（各模式都可能需要）---
    if mode in ("full", "none"):
        _maybe_append(parts, load("identity"))

    if mode == "none":
        return "\n\n".join(parts)

    # --- 2. Soul 价值观与个性（仅 full）---
    if mode == "full":
        _maybe_append(parts, load("soul"))

    # --- 3. Agents 代理行为规范（仅 full）---
    if mode == "full":
        _maybe_append(parts, load("agents"))

    # --- 3.5 Skills 技能（仅 full，默认加载 schedule）---
    if mode == "full":
        _maybe_append(parts, _load_skill("schedule", max_section_chars))

    # --- 4. User 用户画像（仅 full）---
    if mode == "full":
        user_content = _load_user_section(max_section_chars)
        if user_content:
            _maybe_append(parts, user_content)

    # --- 5. Tools 工具指南（full + minimal）---
    if mode in ("full", "minimal"):
        if (tool_mode or "full").lower() == "kernel":
            _maybe_append(parts, load("tools_kernel"))
        else:
            _maybe_append(parts, load("tools"))

    # --- 6. Runtime 运行时信息（full + minimal）---
    if mode in ("full", "minimal"):
        # 6.1 时间上下文
        time_section = load("runtime_time")
        if time_section:
            time_section = time_section.format(time_context=time_context)
            _maybe_append(parts, time_section)

        # 6.2 联网搜索（可选）
        if config.llm.enable_search and config.llm.provider == "qwen":
            web_capabilities = [
                "- 当前新闻、热点事件",
                "- 实时天气信息",
                "- 股票价格、汇率等金融数据",
                "- 最新的技术资讯、行业动态",
                "- 其他需要实时更新的信息",
            ]
            web_search = load("runtime_web_search")
            if web_search:
                web_search = web_search.format(capabilities="\n".join(web_capabilities))
                _maybe_append(parts, web_search)

        # 6.3 网页访问（可选）
        if has_web_extractor:
            _maybe_append(parts, load("runtime_web_extractor"))

        # 6.4 文件读写（可选）
        if has_file_tools:
            _maybe_append(parts, load("runtime_file_tools"))

    return "\n\n".join(parts)
