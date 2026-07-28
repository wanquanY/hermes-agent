from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from hermes_agent.composition.cli_session_store import open_cli_session_store


def _compressor(
    context_length: int,
    *,
    threshold_percent: float = 0.50,
    max_tokens: int | None = None,
) -> ContextCompressor:
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=context_length,
    ):
        return ContextCompressor(
            "test-model",
            threshold_percent=threshold_percent,
            max_tokens=max_tokens,
            quiet_mode=True,
        )


def test_small_context_threshold_floor_and_output_reservation():
    compressor_128k = _compressor(128_000, max_tokens=8_000)
    assert compressor_128k.threshold_percent == 0.75
    assert compressor_128k.threshold_tokens == 90_000

    compressor_256k = _compressor(256_000, max_tokens=16_000)
    assert compressor_256k.threshold_percent == 0.75
    assert compressor_256k.threshold_tokens == 180_000

    # 512K is the documented boundary: it keeps the configured percentage.
    compressor_512k = _compressor(512_000, max_tokens=32_000)
    assert compressor_512k.threshold_percent == 0.50
    assert compressor_512k.threshold_tokens == 240_000


def test_minimum_context_degeneracy_triggers_before_effective_window_exhaustion():
    compressor = _compressor(64_000, max_tokens=8_000)

    assert compressor.threshold_tokens == int(56_000 * 0.85)
    assert 0 < compressor.threshold_tokens < 56_000


def test_model_switch_reapplies_floor_from_configured_percentage():
    compressor = _compressor(600_000, max_tokens=8_000)
    assert compressor.threshold_percent == 0.50

    compressor.update_model("small-model", 128_000)
    assert compressor.threshold_percent == 0.75
    assert compressor.threshold_tokens == 90_000

    compressor.update_model("large-model", 600_000)
    assert compressor.threshold_percent == 0.50
    assert compressor.threshold_tokens == 296_000


def test_compaction_verdict_and_fallback_streak_survive_restart_and_rotation(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    store.sessions.create("parent", "cli")
    try:
        first = _compressor(128_000, max_tokens=8_000)
        first.bind_session_state(store, "parent")
        first.record_completed_compaction(used_fallback=True)

        resumed = _compressor(128_000, max_tokens=8_000)
        resumed.bind_session_state(store, "parent")
        assert resumed._fallback_compression_streak == 1  # noqa: SLF001
        assert resumed._verify_compaction_cleared_threshold is True  # noqa: SLF001

        resumed.update_from_response(
            {
                "prompt_tokens": resumed.threshold_tokens + 1,
                "completion_tokens": 10,
            }
        )
        assert resumed._ineffective_compression_count == 1  # noqa: SLF001
        assert resumed._verify_compaction_cleared_threshold is False  # noqa: SLF001

        resumed.record_completed_compaction(used_fallback=True)
        assert resumed.should_compress(resumed.threshold_tokens + 1) is False

        store.sessions.create(
            "child",
            "cli",
            parent_session_id="parent",
        )
        child = _compressor(128_000, max_tokens=8_000)
        child.on_session_start(
            "child",
            session_db=store,
            old_session_id="parent",
            boundary_reason="compression",
        )
        child_state = store.runtime_stability.get("child")
        assert child_state.compression_ineffective_count == 1
        assert child_state.compression_fallback_streak == 2
        assert child_state.compression_verdict_pending is True
    finally:
        store.close()


def test_usage_less_response_consumes_pending_verdict_once(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    store.sessions.create("session-1", "cli")
    try:
        compressor = _compressor(128_000)
        compressor.bind_session_state(store, "session-1")
        compressor.record_completed_compaction()

        compressor.update_from_response({})
        state = store.runtime_stability.get("session-1")
        assert state.compression_verdict_pending is False
        assert state.compression_ineffective_count == 0

        compressor.update_from_response(
            {"prompt_tokens": compressor.threshold_tokens + 10}
        )
        assert compressor._ineffective_compression_count == 0  # noqa: SLF001
    finally:
        store.close()


def test_compression_failure_cooldown_survives_restart(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    store.sessions.create("session-1", "cli")
    try:
        first = _compressor(128_000)
        first.bind_session_state(store, "session-1")
        first._record_compression_failure_cooldown(30, "temporary outage")  # noqa: SLF001

        resumed = _compressor(128_000)
        resumed.bind_session_state(store, "session-1")
        cooldown = resumed.get_active_compression_failure_cooldown()
        assert cooldown is not None
        assert 0 < cooldown["remaining_seconds"] <= 30
        assert cooldown["error"] == "temporary outage"
        assert resumed.should_compress(resumed.threshold_tokens + 1) is False
    finally:
        store.close()


def test_worker_rpc_mapping_snapshot_restores_compression_state():
    compressor = _compressor(128_000)
    store = SimpleNamespace(
        runtime_stability=SimpleNamespace(
            get=lambda _session_id: {
                "compression_ineffective_count": 1,
                "compression_fallback_streak": 2,
                "compression_verdict_pending": True,
                "compression_failure_cooldown_until": 0.0,
                "compression_failure_error": "",
            }
        )
    )

    compressor.bind_session_state(store, "session-1")

    assert compressor._ineffective_compression_count == 1  # noqa: SLF001
    assert compressor._fallback_compression_streak == 2  # noqa: SLF001
    assert compressor._verify_compaction_cleared_threshold is True  # noqa: SLF001


def test_blocked_gate_refreshes_durable_clear_and_unblocks(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    store.sessions.create("session-1", "cli")
    try:
        compressor = _compressor(128_000)
        compressor.bind_session_state(store, "session-1")
        compressor._fallback_compression_streak = 2  # noqa: SLF001

        store.runtime_stability.write_compression(
            "session-1",
            ineffective_count=0,
            fallback_streak=0,
            verdict_pending=False,
        )

        assert compressor.should_compress(compressor.threshold_tokens + 1) is True
        assert compressor._fallback_compression_streak == 0  # noqa: SLF001
    finally:
        store.close()


def test_failed_local_guard_persist_is_not_cleared_by_empty_durable_row():
    compressor = _compressor(128_000)
    store = SimpleNamespace(
        runtime_stability=SimpleNamespace(
            get=lambda _session_id: {
                "compression_ineffective_count": 0,
                "compression_fallback_streak": 0,
                "compression_verdict_pending": False,
                "compression_failure_cooldown_until": 0.0,
                "compression_failure_error": "",
            }
        )
    )
    compressor.bind_session_state(store, "session-1")
    compressor._summary_failure_cooldown_until = time.monotonic() + 30  # noqa: SLF001
    compressor._last_summary_error = "local persist failed"  # noqa: SLF001
    compressor._compression_state_persist_failed = True  # noqa: SLF001

    cooldown = compressor.get_active_compression_failure_cooldown(refresh=True)

    assert cooldown is not None
    assert cooldown["error"] == "local persist failed"
