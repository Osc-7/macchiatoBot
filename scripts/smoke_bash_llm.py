#!/usr/bin/env python3
"""
真实 LLM API + bash 冒烟（可选 OS 用户 / runuser）。

用法（仓库根）::
  source init.sh
  python scripts/smoke_bash_llm.py
  MACCHIATO_SMOKE_BASH_OS=1 python scripts/smoke_bash_llm.py

需要 ``config/config.yaml`` 中已配置可用的 ``llm.providers``（及 .env 中的 API key）。
默认关闭记忆、推迟 MCP，仅验证一条用户消息 +（若模型配合）bash 工具链。

退出码：0 成功；2 未配置密钥或 LLM 失败；3 bash 子进程不可用。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# 仓库根
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bash-os",
        action="store_true",
        help="启用 command_tools.bash_os_user_enabled（须 Linux + root + runuser）",
    )
    parser.add_argument(
        "--user-id",
        default="smoke_user",
        help="逻辑 user_id（租户工作区与 OS 用户名映射）",
    )
    args = parser.parse_args()
    bash_os = args.bash_os or os.environ.get("MACCHIATO_SMOKE_BASH_OS", "").strip() in (
        "1",
        "true",
        "yes",
    )

    from agent_core.agent.agent import AgentCore
    from agent_core.config import load_config
    from system.kernel import AgentKernel
    from agent_core.interfaces import AgentHooks
    from system.tools import build_tool_registry

    os.chdir(_ROOT)
    cfg = load_config(_ROOT / "config" / "config.yaml")
    if bash_os:
        ct = cfg.command_tools.model_copy(
            update={
                "bash_os_user_enabled": True,
                "bash_os_auto_provision_users": True,
            }
        )
        cfg = cfg.model_copy(update={"command_tools": ct})

    catalog = build_tool_registry(config=cfg, profile=None, filter_by_profile=False)

    core = AgentCore(
        config=cfg,
        tool_catalog=catalog,
        max_iterations=4,
        user_id=args.user_id,
        source="cli",
        defer_mcp_connect=True,
        memory_enabled=False,
    )

    prompt = (
        "请只完成一件事：调用 bash 工具执行命令 "
        "`echo SMOKE_BASH_USER=$(id -un); echo SMOKE_PWD=$(pwd)`，"
        "timeout 用 15。不要在回复里执行其它工具。"
        "最终回复中必须包含两行输出里带 SMOKE_ 的原文。"
    )

    async with core:
        kernel = AgentKernel(tool_registry=core._tool_registry)
        await core.prepare_turn(prompt)
        hooks = AgentHooks()
        result = await kernel.run(core, turn_id=core._current_turn_id, hooks=hooks)
        text = (result.output_text or "").strip()
        print("--- LLM 回复（节选）---")
        print(text[:2000] if len(text) > 2000 else text)

        bash = getattr(core, "_bash", None)
        if bash is None:
            print("--- bash 子进程未启动（command_tools.enabled/allow_run?）", file=sys.stderr)
            return 3
        direct = await bash.execute(
            "echo DIRECT_UID=$(id -u); echo DIRECT_USER=$(id -un); echo DIRECT_PWD=$(pwd)",
            timeout=15.0,
        )
        print("--- 直连 BashRuntime.execute（验证进程身份）---")
        print((direct.stdout + direct.stderr).strip())
        if direct.exit_code != 0:
            return 3

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        raise SystemExit(2) from e
    except Exception as e:
        print(f"失败: {e}", file=sys.stderr)
        raise SystemExit(2) from e
