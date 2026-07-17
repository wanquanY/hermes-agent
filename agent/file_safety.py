"""Shared file safety rules used by both tools and ACP shims."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _hermes_home_path() -> Path:
    """Resolve the active HERMES_HOME (profile-aware) without circular imports."""
    try:
        from hermes_constants import get_hermes_home  # local import to avoid cycles
        return get_hermes_home()
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def _hermes_root_path() -> Path:
    """Resolve the Hermes root dir (always the parent of any profile, never per-profile)."""
    try:
        from hermes_constants import get_default_hermes_root  # local import to avoid cycles
        return get_default_hermes_root()
    except Exception:
        return Path(os.path.expanduser("~/.hermes"))


def build_write_denied_paths(home: str) -> set[str]:
    """Return exact sensitive paths that must never be written."""
    hermes_home = _hermes_home_path()
    hermes_root = _hermes_root_path()
    return {
        os.path.realpath(p)
        for p in [
            os.path.join(home, ".ssh", "authorized_keys"),
            os.path.join(home, ".ssh", "id_rsa"),
            os.path.join(home, ".ssh", "id_ed25519"),
            os.path.join(home, ".ssh", "config"),
            # Active profile .env (or top-level .env when not in profile mode).
            str(hermes_home / ".env"),
            # Top-level .env, even when running under a profile — overwriting it
            # leaks credentials across every profile that inherits from root (#15981).
            str(hermes_root / ".env"),
            os.path.join(home, ".bashrc"),
            os.path.join(home, ".zshrc"),
            os.path.join(home, ".profile"),
            os.path.join(home, ".bash_profile"),
            os.path.join(home, ".zprofile"),
            os.path.join(home, ".netrc"),
            os.path.join(home, ".pgpass"),
            os.path.join(home, ".npmrc"),
            os.path.join(home, ".pypirc"),
            "/etc/sudoers",
            "/etc/passwd",
            "/etc/shadow",
        ]
    }


def build_write_denied_prefixes(home: str) -> list[str]:
    """Return sensitive directory prefixes that must never be written."""
    return [
        os.path.realpath(p) + os.sep
        for p in [
            os.path.join(home, ".ssh"),
            os.path.join(home, ".aws"),
            os.path.join(home, ".gnupg"),
            os.path.join(home, ".kube"),
            "/etc/sudoers.d",
            "/etc/systemd",
            os.path.join(home, ".docker"),
            os.path.join(home, ".azure"),
            os.path.join(home, ".config", "gh"),
        ]
    ]


def get_safe_write_root() -> Optional[str]:
    """Return the resolved HERMES_WRITE_SAFE_ROOT path, or None if unset."""
    root = os.getenv("HERMES_WRITE_SAFE_ROOT", "")
    if not root:
        return None
    try:
        return os.path.realpath(os.path.expanduser(root))
    except Exception:
        return None


def is_write_denied(path: str) -> bool:
    """Return True if path is blocked by the write denylist or safe root."""
    home = os.path.realpath(os.path.expanduser("~"))
    resolved = os.path.realpath(os.path.expanduser(str(path)))

    if resolved in build_write_denied_paths(home):
        return True
    for prefix in build_write_denied_prefixes(home):
        if resolved.startswith(prefix):
            return True

    safe_root = get_safe_write_root()
    if safe_root and not (resolved == safe_root or resolved.startswith(safe_root + os.sep)):
        return True

    return False


_BLOCKED_PROJECT_ENV_BASENAMES = frozenset(
    {".env", ".env.local", ".env.development", ".env.production", ".env.test", ".env.staging", ".envrc"}
)
_HERMES_CREDENTIAL_FILES = (
    "auth.json",
    "auth.lock",
    ".anthropic_oauth.json",
    ".env",
    "webhook_subscriptions.json",
    os.path.join("auth", "google_oauth.json"),
    os.path.join("cache", "bws_cache.json"),
)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def get_read_block_error(path: str) -> Optional[str]:
    """Return a reason when a canonical path is unsafe to place in model context."""
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return f"Access denied: {path} could not be validated against the read policy."

    hermes_dirs: list[Path] = []
    for base in (_hermes_home_path(), _hermes_root_path()):
        try:
            canonical = base.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if canonical not in hermes_dirs:
            hermes_dirs.append(canonical)

    for home in hermes_dirs:
        if _is_within(resolved, home / "skills" / ".hub"):
            return (
                f"Access denied: {path} is an internal Hermes cache file and cannot be read "
                "directly to prevent prompt injection. Use the skills_list or skill_view tools instead."
            )
        if _is_within(resolved, home / "mcp-tokens"):
            return f"Access denied: {path} is Hermes MCP OAuth token material."
        for relative in _HERMES_CREDENTIAL_FILES:
            try:
                blocked = (home / relative).resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved == blocked:
                return f"Access denied: {path} is a Hermes credential store."

    user_home = Path(os.path.expanduser("~")).resolve()
    for relative in (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".config/gh"):
        if _is_within(resolved, user_home / relative):
            return f"Access denied: {path} is a user credential store."
    if resolved.name.lower() in _BLOCKED_PROJECT_ENV_BASENAMES:
        return f"Access denied: {path} is a secret-bearing environment file."
    return None
