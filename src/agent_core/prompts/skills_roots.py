"""Skill root discovery: ``.macchiato/skills`` then ``.agents/skills``.

``npx skills add -g`` installs into ``~/.agents/skills``. Workspace-local
skills may also live under ``.macchiato/skills``. Same-name skills prefer
``.macchiato``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Sequence

# Relative to session / remote workspace home. Order = search priority.
SKILL_ROOT_RELS: tuple[str, ...] = (
    ".macchiato/skills",
    ".agents/skills",
)


def skill_root_paths_under(home: Path) -> List[Path]:
    """Return absolute skill roots under ``home`` (may not exist yet)."""
    base = Path(home).expanduser().resolve()
    return [(base / rel).resolve() for rel in SKILL_ROOT_RELS]


def ensure_skill_roots(home: Path) -> List[Path]:
    """Create skill roots if missing; return existing directories only."""
    out: List[Path] = []
    for root in skill_root_paths_under(home):
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            if not root.is_dir():
                continue
        if root.is_dir():
            out.append(root)
    return out


def merge_skill_roots(
    *,
    home: Path,
    default_cli: Optional[Path] = None,
    prefer_default_cli_as_agents: bool = False,
) -> List[Path]:
    """
    Build the ordered list of skill directories to scan.

    - Always include ``{home}/.macchiato/skills`` when it is (or can be) a dir.
    - Agents root: ``default_cli`` when ``prefer_default_cli_as_agents`` (non-isolated
      host home matching ``skills.cli_dir``), else ``{home}/.agents/skills``.
    """
    roots: List[Path] = []
    seen: set[str] = set()

    def _add(p: Optional[Path]) -> None:
        if p is None:
            return
        try:
            resolved = p.expanduser().resolve()
        except OSError:
            return
        key = str(resolved)
        if key in seen:
            return
        if not resolved.is_dir():
            try:
                resolved.mkdir(parents=True, exist_ok=True)
            except OSError:
                return
        if resolved.is_dir():
            seen.add(key)
            roots.append(resolved)

    mac = (Path(home).expanduser().resolve() / ".macchiato" / "skills")
    _add(mac)

    if prefer_default_cli_as_agents and default_cli is not None:
        _add(default_cli)
    else:
        _add(Path(home).expanduser().resolve() / ".agents" / "skills")

    return roots


def list_skills_in_roots(roots: Sequence[Path]) -> List[str]:
    """List skill directory names that contain SKILL.md; first root wins on name clash."""
    seen: set[str] = set()
    names: List[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in seen:
                continue
            if (d / "SKILL.md").is_file():
                seen.add(d.name)
                names.append(d.name)
    return names


def resolve_skill_md_path(
    skill_name: str, roots: Sequence[Path]
) -> Optional[Path]:
    """Return first matching ``SKILL.md`` across roots."""
    name = (skill_name or "").strip()
    if not name:
        return None
    for root in roots:
        cand = root / name / "SKILL.md"
        if cand.is_file():
            return cand
    return None


def remote_skill_rel_candidates(skill_name: str) -> List[str]:
    """Workspace-relative paths to try on a remote worker (posix)."""
    name = (skill_name or "").strip()
    if not name:
        return []
    return [f"{rel}/{name}/SKILL.md" for rel in SKILL_ROOT_RELS]


def format_skills_index_lines(
    lines: Iterable[str],
    *,
    source_note: str = "",
) -> str:
    """Wrap skill bullet lines into the progressive-disclosure index block."""
    body = "\n".join(line for line in lines if line and str(line).strip())
    if not body.strip():
        return ""
    note = (source_note or "").strip()
    note_block = f"\n{note}\n" if note else "\n"
    return (
        "## Available Skills (Index)\n\n"
        "**Progressive disclosure**: Only names and brief descriptions are shown here to save context. "
        "When a task requires a skill, call `load_skill(skill_name)` to load the full SKILL content, then follow its instructions."
        f"{note_block}"
        f"{body}\n\n"
        "> Call `load_skill(skill_name)` to fetch full skill documentation when needed."
    )
