"""spec §4.6 whole-tree reality check — single owner per aggregate table.

The existing ``test_j4_6_cross_aggregate_isolation.py`` only scans
``hermes_agent/repositories/`` — it proves each RepoImpl stays inside its
own aggregate. But it has a **blind spot**: any *other* file under
``hermes_agent/`` that writes an aggregate table with raw SQL is invisible
to it.

Audit 2026-07-08 found exactly this: ``hermes_agent/storage/cli_session_store.py``
(a P2-era CLI facade, 1057 lines) issues 57 raw ``.execute()`` calls,
writing ``sessions`` / ``session_index`` / ``session_lineage`` / ``messages``
directly — duplicating ``SessionRepoImpl`` and ``MessageRepo`` logic
instead of delegating. Example: ``end_session()`` runs raw
``UPDATE sessions ...`` even though ``SessionRepoImpl.close()`` exists.

That is a NEW shadow writer to aggregate tables, hidden in the
``storage/`` layer where the repositories-only §4.6 guard cannot see it.

This test scans the ENTIRE ``hermes_agent/`` tree and asserts each
aggregate table has exactly one authorized writer file (the aggregate
root, plus explicitly-listed domain services that legitimately cross
tables). Anything else is a shadow writer.

**Marked xfail(strict=False)** — the failure is the reality signal that
illuminates the blind spot. It flips to xpassed when CliSessionStore
(and any other storage-layer writer) converges to pure delegation
through the repositories. Do NOT silence by narrowing scope — instead
route the write through the owning repository.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
V3_PKG = REPO_ROOT / "hermes_agent"


# Aggregate table -> the single set of files authorized to physically write it.
# Everything else in hermes_agent/ that writes these tables is a shadow writer.
_TABLE_OWNERS: dict[str, set[str]] = {
    # Session aggregate — SessionRepo owns; session_deletion domain service
    # legitimately crosses session + lineage tables (documented exception).
    "sessions": {
        "hermes_agent/repositories/session_repo.py",
        "hermes_agent/domain/session_deletion.py",
    },
    "session_index": {
        "hermes_agent/repositories/session_repo.py",
        "hermes_agent/domain/session_deletion.py",
    },
    "session_branches": {"hermes_agent/repositories/session_repo.py"},
    "session_handoffs": {"hermes_agent/repositories/session_repo.py"},
    "session_lineage": {
        "hermes_agent/repositories/session_repo.py",
        "hermes_agent/domain/session_deletion.py",
    },
    "session_branch_requests": {
        "hermes_agent/repositories/session_repo.py",
        "hermes_agent/domain/session_deletion.py",
    },
    # Message aggregate — MessageRepo owns. FTS index maintenance
    # (fts_schema.py triggers) writes messages_fts, not messages.
    "messages": {"hermes_agent/repositories/message_repo.py"},
    # Run aggregate — terminal transitions via run_terminator, non-terminal
    # materialized-view fields via run_repo.
    "runs": {
        "hermes_agent/domain/run_terminator.py",
        "hermes_agent/repositories/run_repo.py",
    },
    "run_events": {"hermes_agent/domain/event_ledger.py"},
    "seq_counter": {"hermes_agent/domain/seq_allocator.py"},
    # Agent profile aggregate.
    "agent_profiles": {"hermes_agent/repositories/agent_profile_repo.py"},
    "agent_profile_versions": {"hermes_agent/repositories/agent_profile_repo.py"},
    "agent_profile_growth_summary": {
        "hermes_agent/repositories/agent_profile_repo.py"
    },
    # Team mission aggregate.
    "team_missions": {"hermes_agent/repositories/team_mission_repo.py"},
    "team_mission_nodes": {"hermes_agent/repositories/team_mission_repo.py"},
    "team_mission_edges": {"hermes_agent/repositories/team_mission_repo.py"},
    "team_mission_run_bindings": {"hermes_agent/repositories/team_mission_repo.py"},
    "activities": {"hermes_agent/repositories/team_mission_repo.py"},
    "activity_commands": {"hermes_agent/repositories/team_mission_repo.py"},
}

_MIGRATION_PREFIX = "hermes_agent/storage/migrations/"


def _write_re(table: str) -> re.Pattern:
    # Match a physical mutation of exactly this table (word-boundary so
    # ``messages`` does not match ``messages_fts``).
    return re.compile(
        r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+" + table + r"\b",
        re.IGNORECASE,
    )


def _iter_v3_files():
    for path in V3_PKG.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def _string_literals(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_ids.add(id(body[0].value))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_ids:
                continue
            out.append(node.value)
    return out


def _find_shadow_table_writers() -> list[tuple[str, str, str]]:
    """Return (rel_path, table, snippet) for every unauthorized aggregate
    write in the hermes_agent/ tree.
    """
    compiled = {t: _write_re(t) for t in _TABLE_OWNERS}
    offenders: list[tuple[str, str, str]] = []
    for path in _iter_v3_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(_MIGRATION_PREFIX):
            continue  # migrations legitimately shape/backfill any table
        literals = _string_literals(path)
        for table, owners in _TABLE_OWNERS.items():
            if rel in owners:
                continue
            pat = compiled[table]
            for literal in literals:
                if pat.search(literal):
                    snippet = literal.strip().replace("\n", " ")[:90]
                    offenders.append((rel, table, snippet))
                    break
    return offenders


# ---------------------------------------------------------------------------


def test_owner_files_exist():
    """Sanity — every declared owner file is a real file."""
    missing = []
    for owners in _TABLE_OWNERS.values():
        for owner in owners:
            if not (REPO_ROOT / owner).exists():
                missing.append(owner)
    assert not missing, f"declared owner files do not exist: {sorted(set(missing))}"


def test_owner_files_actually_write_their_tables():
    """Sanity — each aggregate root file really does contain the write it
    claims to own (guards against the scanner going stale).
    """
    # Spot-check a couple of load-bearing ones.
    session_repo = (REPO_ROOT / "hermes_agent/repositories/session_repo.py").read_text(
        encoding="utf-8"
    )
    assert _write_re("sessions").search(session_repo), (
        "session_repo.py no longer writes sessions — scanner is stale"
    )
    ledger = (REPO_ROOT / "hermes_agent/domain/event_ledger.py").read_text(
        encoding="utf-8"
    )
    assert _write_re("run_events").search(ledger), (
        "event_ledger.py no longer writes run_events — scanner is stale"
    )


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Whole-tree §4.6 reality signal (audit 2026-07-08). Surfaces 3 "
        "shadow-writer files the repositories-only guard could not see:\n"
        "  1. storage/cli_session_store.py — P2 CLI facade writes sessions/"
        "session_index/session_lineage/session_branch_requests/messages with "
        "raw SQL instead of delegating to SessionRepoImpl/MessageRepo (e.g. "
        "end_session() raw-writes sessions though SessionRepoImpl.close() "
        "exists).\n"
        "  2. repositories/message_repo.py — updates sessions.message_count + "
        "session_index.title (cross-aggregate into SessionRepo's tables).\n"
        "  3. domain/session_deletion.py — DELETE FROM messages (cross-"
        "aggregate into MessageRepo's table).\n"
        "Flips to xpassed when each write is routed through its owning "
        "repository, or an owner is added here WITH a documented exception."
    ),
)
def test_no_shadow_writers_to_aggregate_tables_anywhere_in_v3():
    """spec §4.6 REALITY CHECK — whole hermes_agent/ tree.

    Each aggregate table has exactly one authorized writer file. Any other
    file issuing raw INSERT/UPDATE/DELETE against it is a shadow writer —
    the exact anti-pattern this refactor exists to eliminate. Fix by
    routing the write through the owning repository, not by widening the
    owner list.
    """
    offenders = _find_shadow_table_writers()
    if offenders:
        by_file: dict[str, list[str]] = {}
        for rel, table, snippet in offenders:
            by_file.setdefault(rel, []).append(f"{table}: {snippet!r}")
        formatted = "\n".join(
            f"  {rel}\n    " + "\n    ".join(rows) for rel, rows in by_file.items()
        )
        raise AssertionError(
            f"§4.6 shadow writers to aggregate tables ({len(offenders)} sites "
            f"across {len(by_file)} files):\n" + formatted
        )
