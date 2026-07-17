"""Persistence owner for workspace verification evidence."""

from __future__ import annotations

import json

from hermes_agent.domain.verification import VerificationAggregate, VerificationEvidence
from hermes_agent.repositories.base import RepositoryConnection


_MAX_CHANGED_PATHS = 200


def _decode_paths(raw: object) -> tuple[str, ...]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item).strip())[:_MAX_CHANGED_PATHS]


class VerificationRepository:
    """Owns verification state and its append-only evidence ledger."""

    def __init__(self, conn: RepositoryConnection) -> None:
        self._conn = conn

    def _evidence_from_row(self, row: object | None) -> VerificationEvidence | None:
        if row is None:
            return None
        return VerificationEvidence(
            command=str(row["command"] or ""),
            canonical_command=str(row["canonical_command"] or ""),
            kind=str(row["kind"] or "test"),
            scope=str(row["evidence_scope"] or "full"),
            status=str(row["status"] or "failed"),
            exit_code=int(row["exit_code"] or 0),
            cwd=str(row["cwd"] or ""),
            workspace_root=str(row["workspace_root"] or ""),
            scope_id=str(row["scope_id"] or ""),
            output_summary=str(row["output_summary"] or ""),
            event_id=int(row["id"]),
            edit_generation=int(row["edit_generation"] or 0),
            created_at=float(row["created_at"] or 0),
        )

    def get(self, scope_id: str, workspace_root: str) -> VerificationAggregate | None:
        row = self._conn.execute(
            """
            SELECT scope_id, workspace_root, edit_generation,
                   last_verified_generation, last_event_id, changed_paths_json
            FROM verification_workspace_state
            WHERE scope_id = ? AND workspace_root = ?
            """,
            (scope_id, workspace_root),
        ).fetchone()
        if row is None:
            return None
        evidence_row = None
        if row["last_event_id"] is not None:
            evidence_row = self._conn.execute(
                "SELECT * FROM verification_evidence WHERE id = ?",
                (int(row["last_event_id"]),),
            ).fetchone()
        evidence = self._evidence_from_row(evidence_row)
        return VerificationAggregate(
            scope_id=str(row["scope_id"] or ""),
            workspace_root=str(row["workspace_root"] or ""),
            status="unknown",
            edit_generation=int(row["edit_generation"] or 0),
            last_verified_generation=int(row["last_verified_generation"]),
            changed_paths=_decode_paths(row["changed_paths_json"]),
            evidence=evidence,
            event_id=(int(row["last_event_id"]) if row["last_event_id"] is not None else None),
        )

    def mark_edited(
        self,
        scope_id: str,
        workspace_root: str,
        *,
        changed_paths: tuple[str, ...],
        updated_at: float,
    ) -> VerificationAggregate:
        current = self.get(scope_id, workspace_root)
        merged = tuple(
            dict.fromkeys((*((current.changed_paths if current else ())), *changed_paths))
        )[-_MAX_CHANGED_PATHS:]
        self._conn.execute(
            """
            INSERT INTO verification_workspace_state (
                scope_id, workspace_root, edit_generation,
                last_verified_generation, changed_paths_json, updated_at
            ) VALUES (?, ?, 1, -1, ?, ?)
            ON CONFLICT(scope_id, workspace_root) DO UPDATE SET
                edit_generation = verification_workspace_state.edit_generation + 1,
                changed_paths_json = excluded.changed_paths_json,
                updated_at = excluded.updated_at
            """,
            (scope_id, workspace_root, json.dumps(merged), updated_at),
        )
        state = self.get(scope_id, workspace_root)
        if state is None:  # pragma: no cover - INSERT invariant
            raise RuntimeError("verification edit state did not materialize")
        return state

    def record_evidence(
        self,
        evidence: VerificationEvidence,
        *,
        created_at: float,
        retain_count: int,
        retain_after: float,
    ) -> VerificationAggregate:
        self._conn.execute(
            """
            INSERT INTO verification_workspace_state (
                scope_id, workspace_root, edit_generation,
                last_verified_generation, changed_paths_json, updated_at
            ) VALUES (?, ?, 0, -1, '[]', ?)
            ON CONFLICT(scope_id, workspace_root) DO NOTHING
            """,
            (evidence.scope_id, evidence.workspace_root, created_at),
        )
        row = self._conn.execute(
            """
            SELECT edit_generation FROM verification_workspace_state
            WHERE scope_id = ? AND workspace_root = ?
            """,
            (evidence.scope_id, evidence.workspace_root),
        ).fetchone()
        generation = int(row["edit_generation"] if row is not None else 0)
        cursor = self._conn.execute(
            """
            INSERT INTO verification_evidence (
                scope_id, workspace_root, edit_generation, command,
                canonical_command, kind, evidence_scope, status,
                exit_code, cwd, output_summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence.scope_id,
                evidence.workspace_root,
                generation,
                evidence.command,
                evidence.canonical_command,
                evidence.kind,
                evidence.scope,
                evidence.status,
                evidence.exit_code,
                evidence.cwd,
                evidence.output_summary,
                created_at,
            ),
        )
        event_id = int(cursor.lastrowid)
        if evidence.status == "passed":
            self._conn.execute(
                """
                UPDATE verification_workspace_state
                SET last_verified_generation = edit_generation,
                    last_event_id = ?, updated_at = ?
                WHERE scope_id = ? AND workspace_root = ?
                """,
                (event_id, created_at, evidence.scope_id, evidence.workspace_root),
            )
        else:
            self._conn.execute(
                """
                UPDATE verification_workspace_state
                SET last_event_id = ?, updated_at = ?
                WHERE scope_id = ? AND workspace_root = ?
                """,
                (event_id, created_at, evidence.scope_id, evidence.workspace_root),
            )
        self._prune(
            evidence.scope_id,
            evidence.workspace_root,
            retain_count=max(1, int(retain_count)),
            retain_after=retain_after,
        )
        state = self.get(evidence.scope_id, evidence.workspace_root)
        if state is None:  # pragma: no cover - INSERT invariant
            raise RuntimeError("verification evidence state did not materialize")
        return state

    def _prune(
        self,
        scope_id: str,
        workspace_root: str,
        *,
        retain_count: int,
        retain_after: float,
    ) -> None:
        # The aggregate's last_event_id is always protected even if it is old.
        self._conn.execute(
            """
            DELETE FROM verification_evidence
            WHERE scope_id = ? AND workspace_root = ? AND created_at < ?
              AND id != COALESCE((
                  SELECT last_event_id FROM verification_workspace_state
                  WHERE scope_id = ? AND workspace_root = ?
              ), -1)
            """,
            (scope_id, workspace_root, retain_after, scope_id, workspace_root),
        )
        self._conn.execute(
            """
            DELETE FROM verification_evidence
            WHERE scope_id = ? AND workspace_root = ?
              AND id NOT IN (
                  SELECT id FROM verification_evidence
                  WHERE scope_id = ? AND workspace_root = ?
                  ORDER BY id DESC LIMIT ?
              )
            """,
            (scope_id, workspace_root, scope_id, workspace_root, retain_count),
        )


__all__ = ["VerificationRepository"]
