"""Canonical shell-command normalization and command-position projection.

This module does not decide whether an operation needs approval. It owns the
syntax boundary every approval policy consumes: de-obfuscate once, then mark
positions where a POSIX shell starts a new command while ignoring quoted prose.
"""

from __future__ import annotations

import functools
import os
import re
import unicodedata

from tools.ansi_strip import strip_ansi

COMMAND_START_MARKER = "\x1e"

_PATH_TOKEN_STOP = r"""\s'"`;|&<>()"""
_PATH_TAIL = r"(?P<tail>(?:[/\\][^/\\" + _PATH_TOKEN_STOP + r"]*)+)"


@functools.lru_cache(maxsize=64)
def _home_prefix_fold_regex(path: str):
    if not path:
        return None
    components = [component for component in re.split(r"[/\\]+", path) if component]
    if len(components) < 2:
        return None
    body = r"[/\\]+".join(re.escape(component) for component in components)
    return re.compile(r"[/\\]*" + body + _PATH_TAIL)


def _fold_home_prefixes(command: str, paths, replacement: str) -> str:
    seen: set[str] = set()
    for path in sorted((path for path in paths if path), key=len, reverse=True):
        if path in seen:
            continue
        seen.add(path)
        pattern = _home_prefix_fold_regex(path)
        if pattern is not None:
            command = pattern.sub(
                lambda match: replacement + match.group("tail").replace("\\", "/"),
                command,
            )
    return command


def _rewrite_resolved_user_home(command: str) -> str:
    try:
        home = os.path.expanduser("~")
        candidates = (home, os.path.realpath(home), os.environ.get("HOME", ""))
    except Exception:
        return command
    return _fold_home_prefixes(command, candidates, "~")


def _rewrite_resolved_hermes_home(command: str) -> str:
    try:
        from hermes_constants import get_hermes_home

        home = get_hermes_home().expanduser()
        candidates = (str(home), str(home.resolve(strict=False)))
    except Exception:
        return command
    return _fold_home_prefixes(command, candidates, "~/.hermes")


def normalize_command_for_detection(command: str) -> str:
    """Return the single de-obfuscated representation used by all policies."""
    normalized = strip_ansi(str(command or "")).replace("\x00", "")
    normalized = unicodedata.normalize("NFKC", normalized)
    normalized = _rewrite_resolved_hermes_home(normalized)
    normalized = _rewrite_resolved_user_home(normalized)
    normalized = re.sub(r"\\\r?\n", "", normalized)
    normalized = re.sub(r"\$\{IFS\b[^}]*\}|\$IFS\b", " ", normalized)
    normalized = re.sub(r"\\([^\n])", r"\1", normalized)
    normalized = re.sub(r"''|\"\"", "", normalized)
    return normalized


def mark_shell_command_starts(command: str) -> str:
    """Insert a marker at real, unquoted command starts."""
    out: list[str] = []
    quote: str | None = None
    escaped = False
    at_command_start = True
    index = 0

    while index < len(command):
        char = command[index]
        if escaped:
            out.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            out.append(char)
            escaped = True
            index += 1
            continue
        if quote is not None:
            out.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "`":
            out.append(char)
            at_command_start = True
            index += 1
            continue
        if char == "$" and index + 1 < len(command) and command[index + 1] == "(":
            out.extend(("$", "("))
            at_command_start = True
            index += 2
            continue
        if char in "({":
            out.append(char)
            at_command_start = True
            index += 1
            continue
        if char in ";|&\n":
            out.append(char)
            at_command_start = True
            index += 1
            continue
        if at_command_start and char.isspace():
            out.append(char)
            index += 1
            continue
        if at_command_start and char not in ")}":
            out.append(COMMAND_START_MARKER)
            at_command_start = False
        out.append(char)
        index += 1
    return "".join(out)


def command_detection_variants(command: str) -> tuple[str, str]:
    normalized = normalize_command_for_detection(command)
    return normalized, mark_shell_command_starts(normalized)
