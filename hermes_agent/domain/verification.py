"""Pure domain types and command classification for verification evidence."""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex
from typing import Iterable


_SHELL_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;)\s*")


@dataclass(frozen=True)
class VerificationEvidence:
    command: str
    canonical_command: str
    kind: str
    scope: str
    status: str
    exit_code: int
    cwd: str
    workspace_root: str
    scope_id: str
    output_summary: str = ""
    event_id: int | None = None
    edit_generation: int = 0
    created_at: float = 0.0


@dataclass(frozen=True)
class VerificationAggregate:
    scope_id: str
    workspace_root: str
    status: str
    edit_generation: int
    last_verified_generation: int
    changed_paths: tuple[str, ...] = ()
    evidence: VerificationEvidence | None = None
    event_id: int | None = None


@dataclass(frozen=True)
class VerificationRequirement:
    scope_id: str
    workspace_root: str
    status: str
    edit_generation: int
    changed_paths: tuple[str, ...]
    verify_commands: tuple[str, ...]
    attempt: int
    max_attempts: int

    def prompt(self) -> str:
        commands = "; ".join(self.verify_commands)
        paths = ", ".join(self.changed_paths[:20]) or "workspace files"
        return (
            "Verification is required before completing this coding turn. "
            f"The current edit generation is {self.edit_generation}; its status is "
            f"{self.status}. Changed paths: {paths}. Run an appropriate verification "
            f"command ({commands}), fix failures if any, and only then provide the final "
            "answer. This is runtime policy context, not a user message."
        )


def _tokens(command: str) -> list[list[str]]:
    result: list[list[str]] = []
    for segment in _SHELL_SPLIT_RE.split(command.strip()):
        try:
            parsed = shlex.split(segment)
        except ValueError:
            continue
        while parsed and (parsed[0] in {"env", "command", "time", "noglob"} or ("=" in parsed[0] and not parsed[0].startswith("-"))):
            parsed = parsed[1:]
        if len(parsed) >= 2 and parsed[0] == "corepack" and parsed[1] in {"npm", "pnpm", "yarn"}:
            parsed = parsed[1:]
        if parsed:
            result.append([token[2:] if token.startswith("./") else token for token in parsed])
    return result


def _canonical_spellings(command: str) -> list[list[str]]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    spellings = [tokens]
    if len(tokens) >= 3 and tokens[1] == "run" and tokens[0] in {"npm", "pnpm", "yarn", "bun"}:
        spellings.append([tokens[0], tokens[2]])
    if tokens == ["pytest"]:
        spellings.extend(
            [
                ["python", "-m", "pytest"],
                ["python3", "-m", "pytest"],
                ["uv", "run", "pytest"],
                ["poetry", "run", "pytest"],
                ["pipenv", "run", "pytest"],
            ]
        )
    if len(tokens) == 1 and "/" in tokens[0]:
        spellings.extend([["bash", tokens[0]], ["sh", tokens[0]]])
    return spellings


def match_verification_command(
    command: str,
    canonical_commands: Iterable[str],
) -> tuple[str, list[str]] | None:
    for canonical in canonical_commands:
        for segment in _tokens(command):
            for spelling in _canonical_spellings(str(canonical)):
                if segment[: len(spelling)] == spelling:
                    return str(canonical), segment[len(spelling) :]
    return None


def verification_kind(canonical_command: str) -> str:
    lowered = canonical_command.lower()
    if any(word in lowered for word in ("lint", "eslint", "ruff")):
        return "lint"
    if any(word in lowered for word in ("typecheck", "type-check", "tsc", "mypy", "pyright", "ty")):
        return "typecheck"
    if "build" in lowered:
        return "build"
    if "fmt" in lowered or "format" in lowered:
        return "format"
    if "check" in lowered and "test" not in lowered:
        return "check"
    return "test"


def verification_scope(arguments: Iterable[str]) -> str:
    for argument in arguments:
        value = str(argument or "")
        if value.startswith("-") or "=" in value:
            continue
        if (
            "/" in value
            or "\\" in value
            or "::" in value
            or value.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".java"))
        ):
            return "targeted"
    return "full"


__all__ = [
    "VerificationAggregate",
    "VerificationEvidence",
    "VerificationRequirement",
    "match_verification_command",
    "verification_kind",
    "verification_scope",
]
