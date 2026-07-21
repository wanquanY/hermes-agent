from hermes_agent.application.state_repair_service import (
    probe_state_database,
    repair_state_database,
)
from hermes_agent.composition.cli_session_store import open_cli_session_store


def test_probe_and_repair_report_healthy_database(tmp_path):
    db_path = tmp_path / "state.db"
    store = open_cli_session_store(db_path)
    try:
        store.sessions.create("session-1", source="cli")
    finally:
        store.close()

    assert probe_state_database(db_path) is None
    report = repair_state_database(db_path)
    assert report.repaired is True
    assert report.strategy == "already_healthy"
    assert report.backup_path is None


def test_repair_missing_database_fails_without_creating_it(tmp_path):
    db_path = tmp_path / "missing.db"

    report = repair_state_database(db_path)

    assert report.repaired is False
    assert "does not exist" in str(report.error)
    assert not db_path.exists()
