#!/usr/bin/env python3
"""Reject subprocess calls that can inherit the TUI JSON-RPC command pipe.

The TUI gateway and its tools share stdin with a parent process. A descendant
that inherits fd 0 can mutate the shared open-file description and make the
gateway observe a false EOF. This guard scans bundled code plus the plugin
roots that Hermes actually enables and requires an explicit stdin policy.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from typing import Iterator


TUI_CONTEXT_DIRS = ("agent", "tools", "plugins", "tui_gateway")
SKIP_DIR_NAMES = frozenset(
    {"tests", "scripts", "skills", "optional-skills", "hermes_cli", "gateway", "cron"}
)
EXEMPT_MARKER = "noqa: subprocess-stdin"

_CALLS_REQUIRING_STDIN = frozenset(
    {
        ("subprocess", "run"),
        ("subprocess", "Popen"),
        ("subprocess", "call"),
        ("subprocess", "check_output"),
        ("subprocess", "check_call"),
        ("asyncio", "create_subprocess_exec"),
        ("asyncio", "create_subprocess_shell"),
        ("os", "system"),
    }
)
_CALLS_ACCEPTING_INPUT_PIPE = frozenset(
    {
        ("subprocess", "run"),
        ("subprocess", "check_output"),
    }
)


def _canonical_imports(tree: ast.AST) -> dict[str, tuple[str, ...]]:
    aliases: dict[str, tuple[str, ...]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                canonical = tuple(item.name.split("."))
                aliases[item.asname or canonical[0]] = canonical
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = tuple(node.module.split("."))
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = module + (item.name,)
    return aliases


def _call_path(
    expression: ast.expr,
    aliases: dict[str, tuple[str, ...]],
) -> tuple[str, ...] | None:
    if isinstance(expression, ast.Name):
        return aliases.get(expression.id, (expression.id,))
    if isinstance(expression, ast.Attribute):
        parent = _call_path(expression.value, aliases)
        if parent is not None:
            return parent + (expression.attr,)
    return None


def _is_exempt(node: ast.Call, lines: list[str]) -> bool:
    first_line = max(0, node.lineno - 5)
    last_line = node.end_lineno or node.lineno
    return EXEMPT_MARKER in "\n".join(lines[first_line:last_line])


def _has_explicit_stdin_policy(node: ast.Call, target: tuple[str, ...]) -> bool:
    keywords = {keyword.arg for keyword in node.keywords if keyword.arg is not None}
    if "stdin" in keywords:
        return True
    return target in _CALLS_ACCEPTING_INPUT_PIPE and "input" in keywords


def find_subprocess_calls(content: str, filepath: str) -> list[dict[str, object]]:
    """Return unsafe process-spawning calls found in Python *content*."""
    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError as exc:
        return [
            {
                "file": filepath,
                "line": exc.lineno or 1,
                "snippet": f"unable to parse Python source: {exc.msg}",
            }
        ]

    aliases = _canonical_imports(tree)
    lines = content.splitlines()
    violations: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _call_path(node.func, aliases)
        if target not in _CALLS_REQUIRING_STDIN:
            continue
        if _has_explicit_stdin_policy(node, target) or _is_exempt(node, lines):
            continue
        source_line = lines[node.lineno - 1].strip() if lines else ""
        violations.append(
            {
                "file": filepath,
                "line": node.lineno,
                "snippet": source_line[:120],
            }
        )
    return sorted(violations, key=lambda item: int(item["line"]))


def _iter_python_files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for path in root.rglob("*.py"):
        relative_parts = path.relative_to(root).parts[:-1]
        if any(part in SKIP_DIR_NAMES for part in relative_parts):
            continue
        if path.name == "conftest.py":
            continue
        yield path


def _scan_root(root: Path, *, display_root: Path | None = None) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []
    for path in _iter_python_files(root):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if display_root is not None:
            name = str(path.relative_to(display_root))
        else:
            name = str(path)
        violations.extend(find_subprocess_calls(content, name))
    return violations


def enabled_external_plugin_roots(repo_root: Path) -> tuple[Path, ...]:
    """Resolve plugin roots with the same profile and opt-in rules as runtime."""
    sys.path.insert(0, str(repo_root))
    from hermes_constants import get_hermes_home
    from utils import env_var_enabled

    roots = [get_hermes_home() / "plugins"]
    if env_var_enabled("HERMES_ENABLE_PROJECT_PLUGINS"):
        roots.append(Path.cwd() / ".hermes" / "plugins")

    unique: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        resolved = root.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return tuple(unique)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    os.chdir(repo_root)
    violations: list[dict[str, object]] = []

    for directory in TUI_CONTEXT_DIRS:
        violations.extend(_scan_root(repo_root / directory, display_root=repo_root))
    for plugin_root in enabled_external_plugin_roots(repo_root):
        violations.extend(_scan_root(plugin_root))

    if violations:
        print(f"{len(violations)} subprocess calls have no explicit stdin policy:")
        for violation in violations:
            print(
                f"  {violation['file']}:{violation['line']}: "
                f"{violation['snippet']}"
            )
        print(
            f"Use stdin=subprocess.DEVNULL/PIPE, input= where supported, or "
            f"document an intentional interactive call with '{EXEMPT_MARKER}'."
        )
        return 1

    print("All TUI-context subprocess calls have an explicit stdin policy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
