"""PR-1 — Event identity & integrity contract tests.

Covers three new behaviors introduced by PR-1 in
``tui_gateway/services/run_control.py``:

1. ``_ensure_outbound_run_identity`` — run-scoped event frames
   (message.* / reasoning.* / thinking.* / tool.* / subagent.* /
   artifact.* / error) must never leave the main process with an empty
   ``run_id``. The synthesizer mints a deterministic
   ``synthetic-run:{session}:{turn}`` id and flags the frame with
   ``synthetic_run_id=True`` so run/state bookkeeping skips it.

2. ``_sync_canonical_frame_seq`` — after persistence, the canonical
   ``run_events.seq`` (which ``append_run_event`` may bump above the
   requested seq) is written back into the outbound frame + params so
   what subscribers / replay serve is byte-identical.

3. transient marking — a frame that will never gain a canonical seq
   (no persist, or persist failure) is explicitly flagged
   ``transient=True`` so the FE ledger drops it instead of admitting a
   seq replay can never serve.

These are pure unit tests (no DB needed for #1 and #2); #3 drives
``record_event`` with a non-mock ``SessionDB`` so the
``not worker_process and not will_persist`` path is exercised for
real, and a real DB to exercise the persist-failure branch.
"""

from __future__ import annotations

from typing import Any

import pytest

from hermes_state import SessionDB
from tui_gateway.services import run_control as rc


# ── helpers ─────────────────────────────────────────────────────────────────


def _identity_params(
    *,
    event_type: str = "message.delta",
    session: str = "sess-A",
    turn: str = "turn-1",
    run_id: str = "",
    payload_run_id: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a params dict shaped like a record_event caller's payload."""
    payload: dict[str, Any] = {"delta": "hi", "mode": "append"}
    if payload_run_id is not None:
        payload["run_id"] = payload_run_id
    if extra_payload:
        payload.update(extra_payload)
    params: dict[str, Any] = {
        "type": event_type,
        "conversation_session_id": session,
        "turn_id": turn,
        "seq": 7,
        "payload": payload,
    }
    if run_id:
        params["run_id"] = run_id
    return params


def _clear_module_state() -> None:
    """Reset the module-level dicts so tests don't leak seq/state across
    each other (per-file process isolation doesn't help intra-file)."""
    rc._last_seq_by_session.clear()
    rc._run_state_by_id.clear()
    rc._run_ids_by_session.clear()
    rc._events_by_session.clear()


@pytest.fixture(autouse=True)
def _reset_state():
    _clear_module_state()
    yield
    _clear_module_state()


# ── 1. _ensure_outbound_run_identity ────────────────────────────────────────


class TestEnsureOutboundRunIdentity:
    def test_synthesizes_run_id_when_missing(self):
        params = _identity_params()
        rc._ensure_outbound_run_identity(params)

        assert params["run_id"] == "synthetic-run:sess-A:turn-1"
        assert params["synthetic_run_id"] is True
        # payload sub-dict is synced too
        assert params["payload"]["run_id"] == "synthetic-run:sess-A:turn-1"

    def test_synthesizes_turn_id_too_when_both_missing(self):
        # When run_id is empty, turn_id is ALSO synthesized (the contract
        # is: missing run_id → synthesize both; present run_id + missing
        # turn_id → warn only, no synth). Both-missing is the common path.
        params = _identity_params(turn="")
        # also clear run_id from payload to simulate a truly orphan frame
        rc._ensure_outbound_run_identity(params)

        assert params["run_id"] == "synthetic-run:sess-A:orphan"
        assert params["synthetic_run_id"] is True
        assert params["turn_id"] == "synthetic-turn:synthetic-run:sess-A:orphan"
        assert params["payload"]["run_id"] == params["run_id"]
        assert params["payload"]["turn_id"] == params["turn_id"]

    def test_does_not_touch_frame_with_existing_run_id_and_turn_id(self):
        params = _identity_params(run_id="run-real", turn="turn-real")
        rc._ensure_outbound_run_identity(params)

        assert params["run_id"] == "run-real"
        assert params["turn_id"] == "turn-real"
        assert "synthetic_run_id" not in params

    def test_run_id_present_but_turn_missing_only_warns_no_synth(self):
        # run_id present, turn_id missing → diagnostic warning, NO
        # synthesis of run_id or turn_id (overwriting a real turn kept on
        # the runs row would be worse than an empty turn).
        params = _identity_params(run_id="run-real", turn="")
        rc._ensure_outbound_run_identity(params)

        assert params["run_id"] == "run-real"
        assert params["turn_id"] == ""
        assert "synthetic_run_id" not in params

    @pytest.mark.parametrize(
        "event_type",
        [
            "message.delta",
            "message.start",
            "reasoning.delta",
            "thinking.delta",
            "tool.start",
            "tool.progress",
            "subagent.output_delta",
            "artifact.created",
            "error",
        ],
    )
    def test_identity_event_types_trigger_synthesis(self, event_type: str):
        params = _identity_params(event_type=event_type)
        rc._ensure_outbound_run_identity(params)
        assert params["run_id"].startswith("synthetic-run:")
        assert params["synthetic_run_id"] is True

    @pytest.mark.parametrize(
        "event_type",
        [
            "session.info",
            "session.start",
            "approval.request",   # keyed by request_id, not run lane
            "clarify.request",
            "team_mission.runtime.event",
            "",
        ],
    )
    def test_non_identity_event_types_left_untouched(self, event_type: str):
        params = _identity_params(event_type=event_type)
        rc._ensure_outbound_run_identity(params)
        assert "run_id" not in params or not params.get("run_id", "").startswith("synthetic-run:")
        assert "synthetic_run_id" not in params

    def test_non_dict_params_is_noop(self):
        rc._ensure_outbound_run_identity(None)  # type: ignore[arg-type]
        rc._ensure_outbound_run_identity("not-a-dict")  # type: ignore[arg-type]
        # no exception raised = pass

    def test_synthetic_id_is_deterministic_per_session_turn(self):
        # Two orphan frames of the same turn land in ONE synthetic lane,
        # not one-per-event.
        p1 = _identity_params()
        p2 = _identity_params(event_type="reasoning.delta")
        rc._ensure_outbound_run_identity(p1)
        rc._ensure_outbound_run_identity(p2)
        assert p1["run_id"] == p2["run_id"]


# ── 2. _sync_canonical_frame_seq ────────────────────────────────────────────


class TestSyncCanonicalFrameSeq:
    def _frame_and_params(self, *, seq: int = 5) -> tuple[dict, dict]:
        frame = {
            "type": "message.delta",
            "seq": seq,
            "payload": {"delta": "hi", "seq": seq},
        }
        params = {
            "type": "message.delta",
            "seq": seq,
            "payload": {"delta": "hi", "seq": seq},
        }
        return frame, params

    def test_writes_canonical_seq_into_frame_and_params(self):
        frame, params = self._frame_and_params(seq=5)
        saved = {"seq": 42}
        rc._sync_canonical_frame_seq(
            saved, frame=frame, params=params, stable="sess-A",
            run_id="run-1", event_type="message.delta",
        )
        assert frame["seq"] == 42
        assert frame["payload"]["seq"] == 42
        assert params["seq"] == 42
        assert params["payload"]["seq"] == 42

    def test_updates_last_seq_by_session(self):
        frame, params = self._frame_and_params(seq=5)
        rc._sync_canonical_frame_seq(
            {"seq": 99}, frame=frame, params=params, stable="sess-A",
            run_id="run-1", event_type="message.delta",
        )
        assert rc._last_seq_by_session["sess-A"] == 99

    def test_updates_run_state_last_seq(self):
        rc._run_state_by_id["run-1"] = {"last_seq": 10, "status": "running"}
        frame, params = self._frame_and_params(seq=5)
        rc._sync_canonical_frame_seq(
            {"seq": 30}, frame=frame, params=params, stable="sess-A",
            run_id="run-1", event_type="message.delta",
        )
        assert rc._run_state_by_id["run-1"]["last_seq"] == 30

    def test_run_state_last_seq_takes_max_not_overwrite(self):
        # canonical < existing last_seq → keep existing (max).
        rc._run_state_by_id["run-1"] = {"last_seq": 100, "status": "running"}
        frame, params = self._frame_and_params(seq=5)
        rc._sync_canonical_frame_seq(
            {"seq": 50}, frame=frame, params=params, stable="sess-A",
            run_id="run-1", event_type="message.delta",
        )
        assert rc._run_state_by_id["run-1"]["last_seq"] == 100

    def test_noop_when_canonical_equals_outbound(self):
        frame, params = self._frame_and_params(seq=42)
        rc._sync_canonical_frame_seq(
            {"seq": 42}, frame=frame, params=params, stable="sess-A",
            run_id="run-1", event_type="message.delta",
        )
        assert frame["seq"] == 42  # unchanged
        # last_seq_by_session NOT touched (early return before the lock block)
        assert "sess-A" not in rc._last_seq_by_session

    def test_noop_when_canonical_zero(self):
        frame, params = self._frame_and_params(seq=5)
        rc._sync_canonical_frame_seq(
            {"seq": 0}, frame=frame, params=params, stable="sess-A",
            run_id="run-1", event_type="message.delta",
        )
        assert frame["seq"] == 5  # unchanged
        assert "sess-A" not in rc._last_seq_by_session

    def test_noop_when_canonical_negative(self):
        frame, params = self._frame_and_params(seq=5)
        rc._sync_canonical_frame_seq(
            {"seq": -1}, frame=frame, params=params, stable="sess-A",
            run_id="run-1", event_type="message.delta",
        )
        assert frame["seq"] == 5

    def test_noop_when_saved_not_dict(self):
        frame, params = self._frame_and_params(seq=5)
        for bad in (None, [], "string", 42):
            rc._sync_canonical_frame_seq(
                bad, frame=frame, params=params, stable="sess-A",
                run_id="run-1", event_type="message.delta",
            )
        assert frame["seq"] == 5

    def test_noop_when_saved_seq_missing(self):
        frame, params = self._frame_and_params(seq=5)
        rc._sync_canonical_frame_seq(
            {}, frame=frame, params=params, stable="sess-A",
            run_id="run-1", event_type="message.delta",
        )
        assert frame["seq"] == 5

    def test_payload_without_seq_key_not_added(self):
        # frame.payload / params.payload WITHOUT a 'seq' key should not
        # gain one — only existing seq keys are rewritten.
        frame = {"type": "message.delta", "seq": 5, "payload": {"delta": "hi"}}
        params = {"type": "message.delta", "seq": 5, "payload": {"delta": "hi"}}
        rc._sync_canonical_frame_seq(
            {"seq": 9}, frame=frame, params=params, stable="sess-A",
            run_id="run-1", event_type="message.delta",
        )
        assert frame["seq"] == 9
        assert params["seq"] == 9
        assert "seq" not in frame["payload"]
        assert "seq" not in params["payload"]

    def test_empty_stable_does_not_crash(self):
        frame, params = self._frame_and_params(seq=5)
        rc._sync_canonical_frame_seq(
            {"seq": 9}, frame=frame, params=params, stable="",
            run_id="run-1", event_type="message.delta",
        )
        assert frame["seq"] == 9
        # no session-level update since stable is empty
        assert len(rc._last_seq_by_session) == 0

    def test_empty_run_id_does_not_crash(self):
        frame, params = self._frame_and_params(seq=5)
        rc._sync_canonical_frame_seq(
            {"seq": 9}, frame=frame, params=params, stable="sess-A",
            run_id="", event_type="message.delta",
        )
        assert frame["seq"] == 9
        assert rc._last_seq_by_session["sess-A"] == 9


# ── 3. transient marking ────────────────────────────────────────────────────


class TestTransientMarking:
    def test_transient_set_when_no_persist_and_main_process(self, tmp_path):
        # Main process (not worker), persist=False, no db → will_persist is
        # False → frame must be marked transient. record_event mutates the
        # caller's params dict in place (the identity synth path), so we
        # inspect it after the call.
        params = {
            "type": "message.delta",
            "conversation_session_id": "sess-T",
            "run_id": "run-T",
            "turn_id": "turn-T",
            "seq": 1,
            "payload": {"delta": "x"},
        }
        rc.record_event(params, db=None, persist=False)
        assert params.get("transient") is True

    def test_transient_not_set_when_persists(self, tmp_path):
        # With a real DB + persist=True, will_persist is True → no transient.
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess-T", source="test", transient=False)
        params = {
            "type": "message.delta",
            "conversation_session_id": "sess-T",
            "run_id": "run-T",
            "turn_id": "turn-T",
            "seq": 1,
            "payload": {"delta": "x"},
        }
        rc.record_event(params, db=db, persist=True)
        assert "transient" not in params

    def test_persist_failure_marks_transient(self, tmp_path):
        # A real DB whose append_run_event raises → except branch stamps
        # transient on frame + params.
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess-T", source="test", transient=False)

        original = db.append_run_event

        def _boom(*a, **kw):
            raise RuntimeError("simulated persist failure")

        db.append_run_event = _boom  # type: ignore[assignment]
        try:
            params = {
                "type": "message.delta",
                "conversation_session_id": "sess-T",
                "run_id": "run-T",
                "turn_id": "turn-T",
                "seq": 1,
                "payload": {"delta": "x"},
            }
            rc.record_event(params, db=db, persist=True)
            assert params.get("transient") is True
        finally:
            db.append_run_event = original  # type: ignore[assignment]

    def test_synthetic_run_id_frame_does_not_open_active_run(self, tmp_path):
        # A synthetic-run identity frame must NOT trigger the
        # _event_opens_active_run → get_run → _ensure_run path. We verify
        # this indirectly: the frame still flows (no crash) and run_state
        # for the synthetic id is NOT created (the `not synthetic_run_identity`
        # guard at line 1611 skips _run_state_by_id tracking).
        params = {
            "type": "message.delta",  # an run-opening event type
            "conversation_session_id": "sess-T",
            "turn_id": "",  # force synthesis
            "seq": 1,
            "payload": {"delta": "x"},
            # no run_id → _ensure_outbound_run_identity synthesizes one
        }
        rc.record_event(params, db=None, persist=False)

        synth_run_id = params["run_id"]
        assert synth_run_id.startswith("synthetic-run:")
        assert params.get("synthetic_run_id") is True
        # The synthetic id was NOT registered in run state tracking.
        assert synth_run_id not in rc._run_state_by_id

    def test_session_info_does_not_trigger_identity_synthesis(self, tmp_path):
        # Indirect guard: a session.info frame has no run_id/turn_id but
        # must NOT be synthesized (it's not an identity event type).
        params = {
            "type": "session.info",
            "conversation_session_id": "sess-T",
            "seq": 1,
            "payload": {"info": "something"},
        }
        rc.record_event(params, db=None, persist=False)
        assert "run_id" not in params or not params["run_id"]
        assert "synthetic_run_id" not in params
        # session.info is non-identity → still transient (no persist).
        assert params.get("transient") is True


# ── cross-cutting: identity guard integration ──────────────────────────────


class TestIdentityGuardIntegration:
    def test_error_event_synthesizes_identity(self):
        # 'error' is in _RUN_IDENTITY_EVENT_TYPES (not a prefix match) —
        # confirms the explicit-type set path works, not just prefixes.
        params = {
            "type": "error",
            "conversation_session_id": "sess-E",
            "turn_id": "turn-E",
            "seq": 1,
            "payload": {"message": "boom"},
        }
        rc._ensure_outbound_run_identity(params)
        assert params["run_id"] == "synthetic-run:sess-E:turn-E"
        assert params["synthetic_run_id"] is True
        assert params["payload"]["run_id"] == params["run_id"]

    def test_synthetic_run_id_is_stable_string_format(self):
        # The FE timeline keys by run_id; the format must be stable.
        params = _identity_params(session="my-session", turn="turn-42")
        rc._ensure_outbound_run_identity(params)
        assert params["run_id"] == "synthetic-run:my-session:turn-42"

    def test_missing_session_uses_unknown_session_placeholder(self):
        params = {
            "type": "message.delta",
            "turn_id": "turn-1",
            "seq": 1,
            "payload": {"delta": "x"},
            # no conversation_session_id / session_id
        }
        rc._ensure_outbound_run_identity(params)
        assert params["run_id"] == "synthetic-run:unknown-session:turn-1"
