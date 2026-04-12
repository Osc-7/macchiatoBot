"""
Prompt 管理模块

用于加载和组合各类系统提示、用户提示等。
支持三模式组装：full（主 Agent）、minimal（子 Agent）、none（仅身份）。
"""

from .loader import (
    PromptMode,
    build_shuiyuan_system_prompt,
    build_system_prompt,
    resolve_skills_cli_path,
)

__all__ = [
    "build_system_prompt",
    "build_shuiyuan_system_prompt",
    "PromptMode",
    "resolve_skills_cli_path",
]
