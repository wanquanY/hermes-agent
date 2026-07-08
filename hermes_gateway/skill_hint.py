"""Unavailable skill command hints for gateway slash dispatch."""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def skill_slug_from_frontmatter(skill_md: Path) -> tuple[str | None, str | None]:
    """Derive the /command slug and declared frontmatter name from a SKILL.md."""
    try:
        content = skill_md.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.debug("Failed to read skill frontmatter from %s: %s", skill_md, exc)
        return None, None
    if not content.startswith("---"):
        return None, None
    end = content.find("\n---", 3)
    if end < 0:
        return None, None
    declared_name: str | None = None
    for line in content[3:end].splitlines():
        line = line.strip()
        if line.startswith("name:"):
            raw = line.split(":", 1)[1].strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
                raw = raw[1:-1]
            declared_name = raw.strip()
            break
    if not declared_name:
        return None, None
    slug = declared_name.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        return None, declared_name
    return slug, declared_name


def check_unavailable_skill(command_name: str, *, repo_root: Path) -> str | None:
    """Check if a command matches a known-but-inactive skill."""
    normalized = command_name.lower().replace("_", "-")
    try:
        from agent.skill_utils import get_all_skills_dirs, is_excluded_skill_path
        from tools.skills_tool import _get_disabled_skill_names

        disabled = _get_disabled_skill_names()

        for skills_dir in get_all_skills_dirs():
            if not skills_dir.exists():
                continue
            for skill_md in skills_dir.rglob("SKILL.md"):
                if is_excluded_skill_path(skill_md):
                    continue
                slug, declared_name = skill_slug_from_frontmatter(skill_md)
                if not slug or not declared_name:
                    continue
                if slug == normalized and declared_name in disabled:
                    return (
                        f"The **{command_name}** skill is installed but disabled.\n"
                        f"Enable it with: `hermes skills config`"
                    )

        from hermes_constants import get_optional_skills_dir

        optional_dir = get_optional_skills_dir(repo_root / "optional-skills")
        if optional_dir.exists():
            for skill_md in optional_dir.rglob("SKILL.md"):
                if is_excluded_skill_path(skill_md):
                    continue
                slug, _declared = skill_slug_from_frontmatter(skill_md)
                if not slug:
                    continue
                if slug == normalized:
                    rel = skill_md.parent.relative_to(optional_dir)
                    parts = list(rel.parts)
                    install_path = f"official/{'/'.join(parts)}"
                    return (
                        f"The **{command_name}** skill is available but not installed.\n"
                        f"Install it with: `hermes skills install {install_path}`"
                    )
    except Exception as exc:
        logger.debug("Unavailable skill lookup failed for %s: %s", command_name, exc)
    return None
