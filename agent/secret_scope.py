"""Profile-scoped environment resolution for gateway multiplexing.

The multiplexing gateway serves many profiles from one process. Each profile
has its own ``.env`` with its own provider keys and platform tokens, so profile
secrets cannot be merged into process-global ``os.environ`` safely.

This module provides a fail-closed, context-local profile environment. It owns
both credentials and profile policy values because either kind of setting can
cross-contaminate another bot identity. In the normal single-profile
deployment, ``get_profile_env`` remains compatible with ``os.getenv``. In
multiplex mode, unscoped reads fail loudly instead of leaking configuration
from another profile.
"""
from __future__ import annotations

import os
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Dict, Mapping, Optional


_MULTIPLEX_ACTIVE: bool = False


def set_multiplex_active(active: bool) -> None:
    """Mark whether this process is multiplexing multiple profiles."""
    global _MULTIPLEX_ACTIVE
    _MULTIPLEX_ACTIVE = bool(active)


def is_multiplex_active() -> bool:
    """Return whether profile multiplexing is active."""
    return _MULTIPLEX_ACTIVE


_SECRET_SCOPE: ContextVar[Optional[Mapping[str, str]]] = ContextVar(
    "_SECRET_SCOPE", default=None
)


class UnscopedSecretError(RuntimeError):
    """Raised for an unscoped secret read while multiplexing is active."""


def set_secret_scope(secrets: Optional[Mapping[str, str]]) -> Token:
    """Install the current profile's secret mapping and return its reset token."""
    return _SECRET_SCOPE.set(secrets)


def reset_secret_scope(token: Token) -> None:
    """Restore the preceding secret scope."""
    _SECRET_SCOPE.reset(token)


def current_secret_scope() -> Optional[Mapping[str, str]]:
    """Return the active secret mapping, if any."""
    return _SECRET_SCOPE.get()


_GLOBAL_ENV_EXACT = frozenset(
    {
        "HERMES_HOME",
        "HERMES_PROFILE",
        "HERMES_GATEWAY_LOCK_DIR",
        "HERMES_REDACT_SECRETS",
        "_HERMES_GATEWAY",
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TZ",
        "PWD",
        "SHELL",
        "TMPDIR",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "SSL_CERT_FILE",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_BOARD",
    }
)
_GLOBAL_ENV_PREFIXES = (
    "HERMES_KANBAN_",
    "TERMINAL_",
)


def _is_global_env(name: str) -> bool:
    """Return whether an environment variable is deployment-global."""
    return name in _GLOBAL_ENV_EXACT or any(
        name.startswith(prefix) for prefix in _GLOBAL_ENV_PREFIXES
    )


def get_profile_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a setting from the active profile environment.

    Deployment-global values always read from ``os.environ``. A present profile
    scope is authoritative. Without a scope, single-profile deployments retain
    environment fallback while multiplex deployments fail closed.
    """
    if _is_global_env(name):
        value = os.environ.get(name)
        return value if value is not None else default

    scope = _SECRET_SCOPE.get()
    if scope is not None:
        value = scope.get(name)
        return value if value is not None else default

    if _MULTIPLEX_ACTIVE:
        raise UnscopedSecretError(
            f"get_profile_env({name!r}) called without a profile scope while "
            "multiplexing is active. Run this profile setting read inside a "
            "set_secret_scope(...) block."
        )

    value = os.environ.get(name)
    return value if value is not None else default


def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Backward-compatible credential-oriented alias for ``get_profile_env``."""
    return get_profile_env(name, default)


def build_profile_subprocess_env(
    overrides: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Build an environment for a subprocess owned by the current profile.

    A multiplexed gateway must not inherit arbitrary process variables because
    they can contain the active/default profile's provider keys and channel
    credentials.  Only deployment-global variables cross that boundary; the
    active profile scope is then authoritative.  Single-profile processes keep
    the traditional full-environment behavior for compatibility.
    """
    scope = _SECRET_SCOPE.get()
    if scope is None:
        if _MULTIPLEX_ACTIVE:
            raise UnscopedSecretError(
                "build_profile_subprocess_env() called without a profile scope "
                "while multiplexing is active"
            )
        env = dict(os.environ)
    else:
        env = {
            name: value
            for name, value in os.environ.items()
            if _is_global_env(name)
        }
        env.update({str(name): str(value) for name, value in scope.items()})

        # ``HERMES_HOME`` is context-local in multiplex mode, so the process
        # environment may still point at the active/default profile.
        from hermes_constants import get_hermes_home

        env["HERMES_HOME"] = str(get_hermes_home())

    if overrides:
        env.update({str(name): str(value) for name, value in overrides.items()})
    return env


def load_env_file(env_path: Path) -> Dict[str, str]:
    """Parse a ``.env`` file into a mapping without mutating ``os.environ``."""
    secrets: Dict[str, str] = {}
    try:
        text = env_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return secrets

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        secrets[key] = value

    return secrets


def build_profile_secret_scope(hermes_home: Path) -> Dict[str, str]:
    """Load a profile's ``.env`` as an isolated secret scope."""
    return load_env_file(Path(hermes_home) / ".env")
