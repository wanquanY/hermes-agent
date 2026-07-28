from __future__ import annotations

from hermes_agent.composition.cli_session_store import open_cli_session_store


def _seed_run(store, job_id: str, index: int, started_at: float) -> str:
    session_id = f"cron_{job_id}_{index:08d}"
    store.sessions.create(session_id, "cron")
    store.messages.append(session_id, "user", f"run {index} for {job_id}")
    store.messages.append(session_id, "assistant", "done")
    store.sessions.end(session_id, "completed")
    store._conn.execute(
        "UPDATE sessions SET started_at = ? WHERE id = ?",
        (started_at, session_id),
    )
    store._conn.commit()
    return session_id


def test_cron_run_history_is_scoped_enriched_and_newest_first(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        for index in range(5):
            _seed_run(store, "alpha", index, 1_700_000_000 + index * 60)
        _seed_run(store, "beta", 0, 1_800_000_000)
        _seed_run(store, "xalpha", 0, 1_800_000_001)
        _seed_run(store, "alpha2", 0, 1_800_000_002)
        store.sessions.create("cron_alpha_99999999", "cli")

        runs = store.cron_run_history.list("alpha")

        assert [run["id"] for run in runs] == [
            f"cron_alpha_{index:08d}" for index in reversed(range(5))
        ]
        assert runs[0]["preview"].startswith("run 4 for alpha")
        assert runs[0]["last_active"] >= runs[0]["started_at"]
    finally:
        store.close()


def test_cron_run_history_bounds_pages_and_uses_composite_index(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        for index in range(10):
            _seed_run(store, "alpha", index, 1_700_000_000 + index * 60)

        first = store.cron_run_history.list("alpha", limit=4)
        second = store.cron_run_history.list("alpha", limit=4, offset=4)

        assert len(first) == len(second) == 4
        assert {run["id"] for run in first}.isdisjoint(
            run["id"] for run in second
        )
        assert [run["started_at"] for run in first + second] == sorted(
            (run["started_at"] for run in first + second),
            reverse=True,
        )

        plan = store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT s.* FROM sessions s "
            "INDEXED BY idx_sessions_source_id "
            "WHERE s.source = 'cron' AND s.id >= ? AND s.id < ? "
            "ORDER BY s.started_at DESC LIMIT 20",
            ("cron_alpha_", "cron_alpha`"),
        ).fetchall()
        detail = " ".join(str(row[-1]) for row in plan)
        assert "idx_sessions_source_id" in detail
        assert "source=? AND id>? AND id<?" in detail
    finally:
        store.close()
