from __future__ import annotations

import json
import sqlite3

from hermes_gateway.readiness import collect_runtime_readiness


def test_healthy_profile_readiness_is_bounded(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("model: test/model\n", encoding="utf-8")
    with sqlite3.connect(home / "state.db") as connection:
        connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {"telegram": {"state": "connected"}},
        },
        active_api_runs=2,
    )

    assert result["status"] == "ok"
    assert result["checks"]["state_db"]["status"] == "ok"
    assert result["checks"]["background_queues"]["active_api_runs"] == 2
    assert str(home) not in json.dumps(result)


def test_invalid_config_and_corrupt_db_degrade_without_mutation(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("model: [unterminated", encoding="utf-8")
    (home / "state.db").write_bytes(b"not sqlite")
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="",
        runtime_status={"gateway_state": "stopped"},
    )

    assert result["status"] == "degraded"
    assert result["checks"]["config"]["status"] == "degraded"
    assert result["checks"]["state_db"]["status"] == "degraded"
    assert result["checks"]["model"]["status"] == "degraded"
    assert config.read_text(encoding="utf-8") == "model: [unterminated"
