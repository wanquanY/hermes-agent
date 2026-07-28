"""Application policy for fresh, provider-independent verification evidence."""

from __future__ import annotations

import os
from pathlib import Path
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from agent.coding_context import project_facts_for
from hermes_agent.domain.verification import (
    VerificationAggregate,
    VerificationEvidence,
    VerificationRequirement,
    match_verification_command,
    verification_kind,
    verification_scope,
)
from hermes_agent.repositories.verification_repo import VerificationRepository
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork


_DOCUMENT_SUFFIXES = {
    ".adoc", ".asciidoc", ".csv", ".doc", ".docx", ".log", ".markdown",
    ".md", ".mdx", ".org", ".pdf", ".rst", ".text", ".tsv", ".txt",
}
_DOCUMENT_FILENAMES = {
    "authors", "changelog", "code_of_conduct", "contributing", "contributors",
    "copying", "license", "maintainers", "notice", "readme", "security",
}


class VerificationService:
    def __init__(
        self,
        repository: VerificationRepository,
        unit_of_work: SqliteUnitOfWork,
        *,
        clock: Callable[[], float] = time.time,
        facts_resolver: Callable[[str | Path | None], Mapping[str, Any] | None] = project_facts_for,
        retain_count: int = 100,
        retain_days: float = 30.0,
    ) -> None:
        self._repository = repository
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._facts_resolver = facts_resolver
        self._retain_count = max(1, int(retain_count))
        self._retain_seconds = max(1.0, float(retain_days) * 86400.0)

    @staticmethod
    def _scope_id(value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("verification operation requires scope_id")
        return normalized

    def _facts(self, cwd: str | Path | None) -> dict[str, Any] | None:
        raw = self._facts_resolver(cwd)
        if not raw:
            return None
        root = str(raw.get("root") or "").strip()
        if not root:
            return None
        return {
            "root": str(Path(root).expanduser().resolve()),
            "verifyCommands": tuple(str(item) for item in (raw.get("verifyCommands") or ()) if str(item).strip()),
        }

    @staticmethod
    def _normalize_paths(root: str, paths: Iterable[str]) -> tuple[str, ...]:
        root_path = Path(root).resolve()
        normalized: list[str] = []
        for raw in paths:
            value = str(raw or "").strip()
            if not value:
                continue
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = root_path / candidate
            try:
                relative = candidate.resolve(strict=False).relative_to(root_path)
            except (OSError, ValueError):
                continue
            relative_text = relative.as_posix()
            if relative_text and relative_text != ".":
                normalized.append(relative_text)
        return tuple(dict.fromkeys(normalized))[:200]

    @staticmethod
    def _documents_only(paths: Iterable[str]) -> bool:
        values = tuple(paths)
        return bool(values) and all(
            Path(value).suffix.lower() in _DOCUMENT_SUFFIXES
            or Path(value).name.lower().split(".", 1)[0] in _DOCUMENT_FILENAMES
            for value in values
        )

    @staticmethod
    def _status(state: VerificationAggregate | None, *, commands: tuple[str, ...]) -> str:
        if not commands:
            return "not_applicable"
        if state is None or state.edit_generation <= 0:
            return "not_required"
        if VerificationService._documents_only(state.changed_paths):
            return "not_applicable"
        if state.last_verified_generation >= state.edit_generation:
            return "passed"
        evidence = state.evidence
        if evidence is not None and evidence.edit_generation == state.edit_generation and evidence.status == "failed":
            return "failed"
        return "unverified"

    @staticmethod
    def _with_status(state: VerificationAggregate, status: str) -> VerificationAggregate:
        return VerificationAggregate(
            scope_id=state.scope_id,
            workspace_root=state.workspace_root,
            status=status,
            edit_generation=state.edit_generation,
            last_verified_generation=state.last_verified_generation,
            changed_paths=state.changed_paths,
            evidence=state.evidence,
            event_id=state.event_id,
        )

    def mark_edited(
        self,
        scope_id: str,
        cwd: str | Path | None,
        paths: Iterable[str],
    ) -> VerificationAggregate | None:
        facts = self._facts(cwd)
        if facts is None:
            return None
        normalized_scope = self._scope_id(scope_id)
        normalized_paths = self._normalize_paths(facts["root"], paths)
        if not normalized_paths:
            return self.status(normalized_scope, cwd)
        now = float(self._clock())
        state = self._unit_of_work.execute(
            lambda _conn: self._repository.mark_edited(
                normalized_scope,
                facts["root"],
                changed_paths=normalized_paths,
                updated_at=now,
            )
        )
        return self._with_status(state, self._status(state, commands=facts["verifyCommands"]))

    def record_terminal(
        self,
        scope_id: str,
        *,
        command: str,
        cwd: str | Path | None,
        exit_code: int,
        output: str = "",
    ) -> VerificationAggregate | None:
        facts = self._facts(cwd)
        if facts is None:
            return None
        match = match_verification_command(str(command or ""), facts["verifyCommands"])
        if match is None:
            return None
        canonical, arguments = match
        normalized_scope = self._scope_id(scope_id)
        code = int(exit_code)
        now = float(self._clock())
        summary = str(output or "").strip()
        if len(summary) > 2000:
            summary = f"{summary[:950]}\n... output truncated ...\n{summary[-950:]}"
        evidence = VerificationEvidence(
            command=str(command or "").strip(),
            canonical_command=canonical,
            kind=verification_kind(canonical),
            scope=verification_scope(arguments),
            status="passed" if code == 0 else "failed",
            exit_code=code,
            cwd=str(Path(cwd or os.getcwd()).expanduser().resolve()),
            workspace_root=facts["root"],
            scope_id=normalized_scope,
            output_summary=summary,
        )
        state = self._unit_of_work.execute(
            lambda _conn: self._repository.record_evidence(
                evidence,
                created_at=now,
                retain_count=self._retain_count,
                retain_after=now - self._retain_seconds,
            )
        )
        return self._with_status(state, self._status(state, commands=facts["verifyCommands"]))

    def status(self, scope_id: str, cwd: str | Path | None) -> VerificationAggregate | None:
        facts = self._facts(cwd)
        if facts is None:
            return None
        normalized_scope = self._scope_id(scope_id)
        state = self._unit_of_work.read(
            lambda _conn: self._repository.get(normalized_scope, facts["root"])
        )
        if state is None:
            return VerificationAggregate(
                scope_id=normalized_scope,
                workspace_root=facts["root"],
                status=self._status(None, commands=facts["verifyCommands"]),
                edit_generation=0,
                last_verified_generation=-1,
            )
        return self._with_status(state, self._status(state, commands=facts["verifyCommands"]))

    def completion_requirement(
        self,
        scope_id: str,
        cwd: str | Path | None,
        *,
        attempt: int,
        max_attempts: int = 1,
    ) -> VerificationRequirement | None:
        facts = self._facts(cwd)
        if facts is None or attempt >= max(0, int(max_attempts)):
            return None
        state = self.status(scope_id, cwd)
        if state is None or state.status not in {"failed", "unverified"}:
            return None
        return VerificationRequirement(
            scope_id=state.scope_id,
            workspace_root=state.workspace_root,
            status=state.status,
            edit_generation=state.edit_generation,
            changed_paths=state.changed_paths,
            verify_commands=facts["verifyCommands"],
            attempt=max(0, int(attempt)) + 1,
            max_attempts=max(1, int(max_attempts)),
        )


__all__ = ["VerificationService"]
