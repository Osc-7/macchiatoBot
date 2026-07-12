"""Canonical ``.macchiato/`` layout under a workspace root.

Shared by the full bot (local workspace init) and the lightweight remote
worker. Keep this module free of ``agent_core`` imports.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MACCHIATO_DIR_NAME = ".macchiato"

# Subdirectories created under ``.macchiato/`` (relative names).
MACCHIATO_SUBDIRS: tuple[str, ...] = (
    "jobs",
    "journal",
    "rules",
    "skills",
    "scratch",
)

JOBS_REL = f"{MACCHIATO_DIR_NAME}/jobs"
JOURNAL_REL = f"{MACCHIATO_DIR_NAME}/journal"
RULES_REL = f"{MACCHIATO_DIR_NAME}/rules"
SKILLS_REL = f"{MACCHIATO_DIR_NAME}/skills"
SCRATCH_REL = f"{MACCHIATO_DIR_NAME}/scratch"
DEVICE_MD_REL = f"{MACCHIATO_DIR_NAME}/DEVICE.md"

_DEVICE_MD_TEMPLATE = """# 本机 / 本工作区设备笔记
{device_line}
此文件描述**当前工作区所在机器**的约定，不是跨设备共享的长期记忆。

- 工作区根：本目录的上一级（``.macchiato/`` 的父目录）
- 日记：`.macchiato/journal/YYYY-MM-DD.md`
- 本机规则：`.macchiato/rules/`
- 本机 / 本工作区技能：`.macchiato/skills/`
- 临时稿：`.macchiato/scratch/`
- 后台 job 日志：`.macchiato/jobs/`

跨设备稳定的偏好与约束请写 **MEMORY.md**（由记忆系统映射到 canonical 路径），
设备相关路径与环境写在本文件或日记里，整理记忆时再提炼进 MEMORY 的「设备笔记」分区。
"""


def macchiato_root(workspace_root: Path | str) -> Path:
    """Return ``{workspace_root}/.macchiato``."""
    return Path(workspace_root).expanduser().resolve() / MACCHIATO_DIR_NAME


def resolve_macchiato_paths(workspace_root: Path | str) -> Dict[str, str]:
    """Resolve absolute paths for the standard ``.macchiato/`` tree."""
    root = Path(workspace_root).expanduser().resolve()
    base = root / MACCHIATO_DIR_NAME
    return {
        "workspace_root": str(root),
        "macchiato_dir": str(base),
        "jobs_dir": str(base / "jobs"),
        "journal_dir": str(base / "journal"),
        "rules_dir": str(base / "rules"),
        "skills_dir": str(base / "skills"),
        "scratch_dir": str(base / "scratch"),
        "device_md": str(base / "DEVICE.md"),
    }


def ensure_macchiato_layout(
    workspace_root: Path | str,
    *,
    device_label: Optional[str] = None,
    write_device_md: bool = True,
) -> Dict[str, Any]:
    """
    Create the standard ``.macchiato/`` tree under ``workspace_root`` (idempotent).

    Returns path map plus ``created_paths`` for newly created directories/files.
    Does not overwrite an existing ``DEVICE.md``.
    """
    root = Path(workspace_root).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError(f"workspace_root 已存在且不是目录: {root}")
    root.mkdir(parents=True, exist_ok=True)

    paths = resolve_macchiato_paths(root)
    created_paths: List[str] = []
    base = Path(paths["macchiato_dir"])
    if not base.exists():
        base.mkdir(parents=True, exist_ok=True)
        created_paths.append(str(base))
    elif not base.is_dir():
        raise ValueError(f".macchiato 已存在且不是目录: {base}")

    for name in MACCHIATO_SUBDIRS:
        sub = base / name
        if sub.exists():
            if not sub.is_dir():
                raise ValueError(f".macchiato/{name} 已存在且不是目录: {sub}")
            continue
        sub.mkdir(parents=True, exist_ok=True)
        created_paths.append(str(sub))

    device_md = Path(paths["device_md"])
    if write_device_md and not device_md.exists():
        label = (device_label or "").strip()
        device_line = f"\n设备标签: {label}\n" if label else "\n"
        device_md.write_text(
            _DEVICE_MD_TEMPLATE.format(device_line=device_line),
            encoding="utf-8",
        )
        created_paths.append(str(device_md))

    return {
        **paths,
        "created_paths": created_paths,
    }
