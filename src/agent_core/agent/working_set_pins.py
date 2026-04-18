"""根据 Config + CoreProfile + source 计算 ToolWorkingSetManager 的 pinned 工具名。"""

from __future__ import annotations

from typing import Any, List, Optional

from agent_core.config import Config


def compute_pinned_tool_names_for_core(
    config: Config,
    core_profile: Optional[Any],
    source: str,
) -> List[str]:
    """
    与 AgentCore.__init__ 一致：按模板与 mode 拼装常驻 pinned 列表（含 search/call/bash 等必需项）。
    """
    tools_cfg = config.tools
    template_name = (
        getattr(core_profile, "tool_template", None)
        or ("shuiyuan" if (source or "").strip() == "shuiyuan" else "default")
    )
    template = tools_cfg.get_template(template_name)
    exposure_mode = getattr(
        core_profile, "tool_exposure_mode", template.exposure
    ) or template.exposure
    pinned_tools = list(tools_cfg.core_tools or [])
    if exposure_mode == "pinned":
        pinned_tools.extend(tools_cfg.pinned_tools or [])
    pinned_tools.extend(template.extra or [])
    deduped_pinned: List[str] = []
    for name in pinned_tools:
        norm = str(name).strip()
        if norm and norm not in deduped_pinned:
            deduped_pinned.append(norm)
    _required_in_working_set = ("search_tools", "call_tool", "bash")
    for req in _required_in_working_set:
        if req not in deduped_pinned:
            deduped_pinned.append(req)
    _mode = getattr(core_profile, "mode", None) if core_profile is not None else None
    if _mode != "background":
        if "request_permission" not in deduped_pinned:
            deduped_pinned.append("request_permission")
        if "ask_user" not in deduped_pinned:
            deduped_pinned.append("ask_user")
    return deduped_pinned
