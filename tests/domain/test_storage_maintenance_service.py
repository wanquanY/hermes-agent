from __future__ import annotations

import time

from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_repair_removes_only_dangling_non_authoritative_records(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        store._conn.execute("PRAGMA foreign_keys=OFF")  # noqa: SLF001
        store._conn.execute(  # noqa: SLF001
            """
            INSERT INTO session_lineage (
                session_id, parent_session_id, root_session_id,
                branch_origin, branch_mode, branch_depth, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "missing-branch",
                "missing-parent",
                "missing-root",
                "user_message_action",
                "materialized_prefix",
                1,
                time.time(),
            ),
        )
        store._conn.execute(  # noqa: SLF001
            """
            INSERT INTO session_branch_requests (
                idempotency_key, source_session_id, branch_fingerprint,
                result_session_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "branch-key-orphan",
                "missing-source",
                "fingerprint",
                "missing-result",
                time.time(),
            ),
        )
        store._conn.execute(  # noqa: SLF001
            """
            INSERT INTO team_capability_snapshot_bindings (
                binding_id, mission_id, conversation_id, snapshot_id,
                snapshot_version, source_digest, pinned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "binding:missing",
                "missing-mission",
                "missing-conversation",
                "missing-snapshot",
                1,
                "digest",
                time.time(),
            ),
        )
        store._conn.execute("PRAGMA foreign_keys=ON")  # noqa: SLF001

        repaired = store.maintenance.repair_orphaned_foreign_key_rows()

        assert repaired == 3
        assert store._conn.execute("PRAGMA foreign_key_check").fetchall() == []  # noqa: SLF001
    finally:
        store.close()


def test_auto_prune_reports_stable_result_contract(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        store.sessions.create("old-session", source="cli")
        store.sessions.end("old-session", "user_exit")
        store._conn.execute(  # noqa: SLF001
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 100 * 86400, "old-session"),
        )
        store._conn.commit()  # noqa: SLF001

        first = store.maintenance.maybe_auto_prune_and_vacuum(
            retention_days=90,
            vacuum=False,
        )
        second = store.maintenance.maybe_auto_prune_and_vacuum(
            retention_days=90,
            vacuum=False,
        )

        assert first == {"skipped": False, "pruned": 1, "vacuumed": False}
        assert second == {
            "skipped": True,
            "pruned": 0,
            "vacuumed": False,
            "reason": "interval",
        }
    finally:
        store.close()
