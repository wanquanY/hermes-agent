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

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ZERO_DEBT_DIR = REPO_ROOT / "scripts" / "zero_debt"
if str(ZERO_DEBT_DIR) not in sys.path:
    sys.path.insert(0, str(ZERO_DEBT_DIR))

from aggregate_table_owners import (
    REPO_ROOT,
    TABLE_OWNERS,
    find_shadow_table_writers,
    format_shadow_table_writers,
    write_re,
)


# ---------------------------------------------------------------------------


def test_owner_files_exist():
    """Sanity — every declared owner file is a real file."""
    missing = []
    for owners in TABLE_OWNERS.values():
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
    assert write_re("sessions").search(session_repo), (
        "session_repo.py no longer writes sessions — scanner is stale"
    )
    ledger = (REPO_ROOT / "hermes_agent/domain/event_ledger.py").read_text(
        encoding="utf-8"
    )
    assert write_re("run_events").search(ledger), (
        "event_ledger.py no longer writes run_events — scanner is stale"
    )


def test_no_shadow_writers_to_aggregate_tables_anywhere_in_v3():
    """spec §4.6 REALITY CHECK — whole hermes_agent/ tree.

    Each aggregate table has exactly one authorized writer file. Any other
    file issuing raw INSERT/UPDATE/DELETE against it is a shadow writer —
    the exact anti-pattern this refactor exists to eliminate. Fix by
    routing the write through the owning repository, not by widening the
    owner list.
    """
    offenders = find_shadow_table_writers()
    if offenders:
        raise AssertionError(
            f"§4.6 shadow writers to aggregate tables ({len(offenders)} sites "
            f"across {len({rel for rel, _, _ in offenders})} files):\n"
            + format_shadow_table_writers(offenders)
        )
