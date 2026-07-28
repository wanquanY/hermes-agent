"""Pure security policy for user-configured MCP server entries.

MCP stdio transports intentionally support arbitrary local commands.  This
module therefore does not attempt to whitelist commands; it rejects a small
set of high-signal exfiltration, persistence, and known-compromise shapes.

The policy lives in the domain layer so every adapter applies the same rules:
CLI/dashboard writes, config migration, runtime config loading, and explicit
runtime registration.  In particular, runtime callers must not make this
module an optional import: inability to load the policy is a startup failure,
not permission to spawn an unchecked command.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from typing import Any

_SHELL_INTERPRETERS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "dash",
        "fish",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
    }
)

_EGRESS_PATTERN = re.compile(
    r"(?<![\w.-])(?:curl|wget|nc|ncat|socat)(?![\w.-])"
    r"|/dev/tcp/"
    r"|\bInvoke-WebRequest\b"
    r"|\bInvoke-RestMethod\b"
    r"|\bSystem\.Net\.WebClient\b",
    re.IGNORECASE,
)

_EXFIL_HINT_PATTERN = re.compile(
    r"\.env\b|--data-binary|--data-raw|\b-X\s+POST\b|\bPOST\b|<\s*[^\s]+",
    re.IGNORECASE,
)

_PERSISTENCE_PATTERN = re.compile(
    r"authorized_keys"
    r"|\.ssh/"
    r"|/etc/ssh\b"
    r"|/etc/pam\.d\b|pam_[\w-]+\.so"
    r"|/etc/sudoers"
    r"|/etc/cron|crontab\b"
    r"|/etc/rc\.local|/etc/systemd"
    r"|\.bashrc\b|\.bash_profile\b|\.profile\b|\.zshrc\b",
    re.IGNORECASE,
)

_IOC_SUBSTRINGS = (
    "AAAAC3NzaC1lZDI1NTE5AAAAICBoh1oDC4DnsO1m5mJ4yfEKrQebaFh",
    "hermes-0day",
    "60.165.167.",
    "118.182.244.156",
    "61.178.123.196",
)


def _command_basename(command: Any) -> str:
    text = str(command or "").strip()
    if not text:
        return ""
    try:
        parts = shlex.split(text, posix=(os.name != "nt"))
    except ValueError:
        parts = text.split()
    first = parts[0] if parts else text
    return os.path.basename(first).lower()


def _inline_script(args: Any) -> str:
    if args is None:
        return ""
    if isinstance(args, (list, tuple)):
        return " ".join(str(item) for item in args)
    return str(args)


def _entry_text(entry: Mapping[str, Any]) -> str:
    parts: list[str] = [str(entry.get("command") or "")]
    parts.append(_inline_script(entry.get("args")))
    env = entry.get("env")
    if isinstance(env, dict):
        parts.extend(str(value) for value in env.values())
    return " ".join(parts)


def validate_mcp_server_entry(name: str, entry: object) -> list[str]:
    """Return policy violations for one configured MCP server.

    An empty list means the entry is allowed by this security policy.  Schema
    validation remains a separate concern, but malformed top-level entries are
    rejected here too because runtime filtering is the final spawn boundary.
    """
    if not isinstance(entry, dict):
        return [f"MCP server '{name}' must be configured as an object"]

    flat = _entry_text(entry)
    for ioc in _IOC_SUBSTRINGS:
        if ioc in flat:
            return [
                f"MCP server '{name}' contains a known hermes-0day "
                f"indicator-of-compromise ('{ioc}')"
            ]

    command = entry.get("command")
    basename = _command_basename(command)
    if basename not in _SHELL_INTERPRETERS:
        return []

    script = _inline_script(entry.get("args"))
    if not script:
        return []

    issues: list[str] = []
    if _EGRESS_PATTERN.search(script):
        issue = (
            f"MCP server '{name}' uses shell interpreter '{command}' with "
            "network egress in args"
        )
        if _EXFIL_HINT_PATTERN.search(script):
            issue += " and exfiltration-shaped arguments"
        issues.append(issue)

    if _PERSISTENCE_PATTERN.search(script):
        issues.append(
            f"MCP server '{name}' uses shell interpreter '{command}' to write "
            "to an OS persistence surface (SSH keys / PAM / sudoers / cron / "
            "shell rc) — this is the hermes-0day backdoor shape, not a real "
            "MCP server"
        )

    return issues


def partition_mcp_server_entries(
    servers: Mapping[str, object],
) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, ...]]]:
    """Partition configured servers into spawn-safe and rejected mappings."""
    safe: dict[str, dict[str, Any]] = {}
    rejected: dict[str, tuple[str, ...]] = {}
    for raw_name, entry in servers.items():
        name = str(raw_name)
        issues = validate_mcp_server_entry(name, entry)
        if issues:
            rejected[name] = tuple(issues)
            continue
        # ``validate_mcp_server_entry`` rejects non-dicts above.
        safe[name] = entry  # type: ignore[assignment]
    return safe, rejected


def is_mcp_server_entry_suspicious(name: str, entry: object) -> bool:
    return bool(validate_mcp_server_entry(name, entry))


__all__ = [
    "is_mcp_server_entry_suspicious",
    "partition_mcp_server_entries",
    "validate_mcp_server_entry",
]
