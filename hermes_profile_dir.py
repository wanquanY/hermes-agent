"""Profile directory compatibility helpers."""

from __future__ import annotations

from pathlib import Path


def resolve_default_agent_dir(home_root: Path) -> Path:
    """Return the existing directory for the default agent profile."""
    try:
        root = Path(home_root)
        profiles_root = root / "profiles"
        default_dir = profiles_root / "default"
        legacy_dir = profiles_root / "agent"
        if default_dir.exists():
            default_dir.mkdir(parents=True, exist_ok=True)
            return default_dir
        if legacy_dir.exists():
            legacy_dir.mkdir(parents=True, exist_ok=True)
            return legacy_dir
        default_dir.mkdir(parents=True, exist_ok=True)
        return default_dir
    except Exception:
        fallback = Path(home_root) / "profiles" / "default"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return fallback


def is_legacy_default_agent_dir(home_root: Path) -> bool:
    """True iff only the legacy default profile directory exists."""
    try:
        root = Path(home_root)
        return (root / "profiles" / "agent").exists() and not (
            root / "profiles" / "default"
        ).exists()
    except Exception:
        return False
