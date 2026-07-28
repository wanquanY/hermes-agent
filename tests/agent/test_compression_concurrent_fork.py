"""Regression: prevent transcript fork when two paths compress the same session_id.

Damien's incident (Discord, 2026-05-28): a long Hermes session in a Discord
gateway hit the compression threshold at the end of a turn.  The parent agent
finished delivering the response and ``conversation_loop.py`` fired
``_spawn_background_review(...)`` — which builds a forked ``AIAgent`` that
inherits ``agent.session_id`` (see ``agent/background_review.py``::
``review_agent.session_id = agent.session_id``).  Roughly two seconds later
a synthetic ``Background process proc_… completed`` event arrived and
started a fresh turn on the same parent ``session_id`` (still cached in the
gateway's ``SessionEntry``).  Both paths hit preflight compression on the
same parent transcript and called ``_compress_context`` concurrently.  Each
ended the parent and created its own CHILD session in ``state.db``, both
parented to the same old id.  The gateway's ``SessionEntry`` only caught one
rotation; the other child became an orphan that silently accumulated writes.

Repro shape on Damien's machine:

  parent 20260527_234659_e65f0e  ended_at=set  end_reason='compression'
  child  20260528_113619_fc80e1  parent=20260527_234659_e65f0e  (in SessionEntry)
  child  <orphan>                parent=20260527_234659_e65f0e  (silent writes)

This regression simulates the two concurrent ``compress_context`` calls
against a shared ``state.db`` and asserts that the per-session compression
lock added in this PR prevents the orphan child.  Without the lock the
fixture deterministically produces 2 children; with the lock, exactly 1.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store


def test_restored_anchor_preserves_strict_role_alternation() -> None:
    from agent.conversation_compression import _insert_real_user_anchor

    compressed = [
        {
            "role": "user",
            "content": "[Your active task list was preserved across context compression]",
            "_todo_snapshot_synthetic": True,
        }
    ]
    _insert_real_user_anchor(
        compressed,
        {"role": "user", "content": "REAL HUMAN ASK"},
    )

    assert len(compressed) == 1
    assert compressed[0]["content"].startswith("REAL HUMAN ASK")
    assert not compressed[0].get("_todo_snapshot_synthetic")


def test_user_role_summary_is_not_a_human_anchor() -> None:
    from agent.context_compressor import SUMMARY_PREFIX
    from agent.conversation_compression import _is_real_user_message

    summary = {
        "role": "user",
        "content": f"{SUMMARY_PREFIX}\n## Historical Task Snapshot\nUser asked: x",
    }

    assert not _is_real_user_message(summary)
    assert _is_real_user_message(
        {"role": "user", "content": "please continue"}
    )


def _build_agent_with_db(db: CliSessionStore, session_id: str):
    """Build an AIAgent that's wired to ``db`` and pinned to ``session_id``."""
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}), patch(
        "agent.model_metadata.get_model_context_length",
        return_value=128_000,
    ):
        from run_agent import AIAgent

        # ContextCompressor imports the resolver into its own module namespace,
        # so isolate that reference as well after run_agent has loaded it.
        with patch(
            "agent.context_compressor.get_model_context_length",
            return_value=128_000,
        ):
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=db,
                session_id=session_id,
                skip_context_files=True,
                skip_memory=True,
            )

    # Stub the compressor so it returns deterministic output and DOESN'T make
    # an LLM call.  Sleep inside compress() so the two threads' rotations
    # actually overlap — without that the OS could happen to serialize them
    # and hide the bug.
    compressor = MagicMock()

    def _compress_with_overlap(*_a, **_kw):
        time.sleep(0.25)
        return [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            {"role": "user", "content": "tail"},
        ]

    compressor.compress.side_effect = _compress_with_overlap
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    return agent


def _count_children(db: CliSessionStore, parent_sid: str) -> int:
    """Count rows in state.db whose parent_session_id == parent_sid."""
    rows = db._conn.execute(
        "SELECT id FROM sessions WHERE parent_session_id = ?",
        (parent_sid,),
    ).fetchall()
    return len(rows)


def test_concurrent_compression_does_not_fork_session(tmp_path: Path) -> None:
    """Two AIAgents that share a session_id MUST NOT both rotate it.

    Without the per-session compression lock this fixture deterministically
    produces 2 child sessions (transcript fork).  With the lock the second
    path aborts cleanly, leaving exactly 1 canonical child.
    """
    db = open_cli_session_store(db_path=tmp_path / "state.db")

    parent_sid = "PARENT_TEST_SESSION"
    db.sessions.create(parent_sid, source="discord")

    # Two agents on the same session_id, both wired to the same db —
    # mirrors the parent-turn agent + the background-review fork right
    # after a turn ends.
    agent_a = _build_agent_with_db(db, parent_sid)
    agent_b = _build_agent_with_db(db, parent_sid)
    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]

    def run(agent):
        try:
            agent._compress_context(messages, "sys", approx_tokens=120_000)
        except Exception:
            # Surface to the test if either raises — should not happen.
            raise

    t_a = threading.Thread(target=run, args=(agent_a,), name="main_turn")
    t_b = threading.Thread(target=run, args=(agent_b,), name="review_fork")
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    # Exactly one canonical child — not two orphans.
    assert _count_children(db, parent_sid) == 1, (
        "Compression lock failed: parent session has multiple children in state.db "
        "(transcript fork). This is Damien's incident shape — see the test docstring."
    )

    # And exactly one of the two agents actually rotated its session_id; the
    # other should still hold the parent_sid (its compression was skipped).
    rotated = sum(
        1 for a in (agent_a, agent_b) if a.session_id != parent_sid
    )
    assert rotated == 1, (
        f"Expected exactly one agent to rotate session_id, got {rotated}. "
        "Both agents rotating means the lock didn't serialize them."
    )

    # The lock must be released after the winner finished.
    assert db.compression_leases.holder(parent_sid) is None, (
        "Compression lock leaked: still held after both rotations completed."
    )


def test_skipped_compression_returns_messages_unchanged(tmp_path: Path) -> None:
    """The loser of the lock race must return its input messages verbatim.

    Callers (preflight compression in ``conversation_loop.py``) detect the
    no-op via ``len(returned) == len(input)`` and stop the auto-compress
    retry loop.  If the skipped path returned the compressed view, that
    detection would break and the caller would mutate the conversation
    without going through state.db rotation.
    """
    db = open_cli_session_store(db_path=tmp_path / "state.db")
    parent_sid = "LOSER_TEST"
    db.sessions.create(parent_sid, source="discord")

    # Pre-acquire the lock so the agent's compress_context sees it held.
    held = db.compression_leases.try_acquire(parent_sid, "external_holder")
    assert held is True

    agent = _build_agent_with_db(db, parent_sid)
    messages = [{"role": "user", "content": "m1"}, {"role": "user", "content": "m2"}]

    compressed, _sp = agent._compress_context(messages, "sys", approx_tokens=120_000)

    # Skipped: messages returned verbatim, no rotation
    assert compressed is messages or compressed == messages
    assert agent.session_id == parent_sid
    # Compressor was never called (the skip happens before .compress())
    agent.context_compressor.compress.assert_not_called()


def test_slow_compression_refreshes_lease_until_rotation_finishes(tmp_path: Path) -> None:
    """A summarizer slower than the original TTL must retain exclusivity."""
    db = open_cli_session_store(db_path=tmp_path / "state.db")
    parent_sid = "SLOW_SUMMARY_SESSION"
    db.sessions.create(parent_sid, source="discord")
    winner = _build_agent_with_db(db, parent_sid)
    loser = _build_agent_with_db(db, parent_sid)
    for agent in (winner, loser):
        agent._compression_lock_ttl_seconds = 0.12
        agent._compression_lock_refresh_interval = 0.03
    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]

    thread = threading.Thread(
        target=lambda: winner._compress_context(messages, "sys", approx_tokens=120_000)
    )
    thread.start()
    time.sleep(0.16)  # past the initial lease TTL, while winner still summarizes
    loser._compress_context(messages, "sys", approx_tokens=120_000)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert _count_children(db, parent_sid) == 1
    assert winner.session_id != parent_sid
    assert loser.session_id == parent_sid


def test_delayed_contender_does_not_recompress_rotated_parent(tmp_path: Path) -> None:
    """A lease acquired after the winner exits must still reject stale ownership."""
    db = open_cli_session_store(db_path=tmp_path / "state.db")
    parent_sid = "ALREADY_ROTATED_PARENT"
    db.sessions.create(parent_sid, source="discord")
    db.sessions.end(parent_sid, "compression")
    agent = _build_agent_with_db(db, parent_sid)
    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]

    compressed, _prompt = agent._compress_context(
        messages,
        "sys",
        approx_tokens=120_000,
    )

    assert compressed is messages
    assert agent.session_id == parent_sid
    assert _count_children(db, parent_sid) == 0
    agent.context_compressor.compress.assert_not_called()
    assert db.compression_leases.holder(parent_sid) is None


class _NoLeaseSubsystemDB:
    """Wrap a real store while omitting the required lease component."""

    def __init__(self, real_db: CliSessionStore) -> None:
        self._real = real_db

    def __getattr__(self, name):
        if name == "compression_leases":
            raise AttributeError("compression_leases")
        return getattr(self._real, name)


def test_missing_lease_subsystem_fails_closed(tmp_path: Path) -> None:
    """A configured store without durable lease ownership is invalid."""
    db = open_cli_session_store(db_path=tmp_path / "state.db")
    parent_sid = "SKEW_TEST_SESSION"
    db.sessions.create(parent_sid, source="discord")

    agent = _build_agent_with_db(db, parent_sid)
    agent._session_db = _NoLeaseSubsystemDB(db)

    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]

    with pytest.raises(AttributeError, match="compression_leases"):
        agent._compress_context(messages, "sys", approx_tokens=120_000)

    agent.context_compressor.compress.assert_not_called()
    assert agent.session_id == parent_sid
    assert agent._compression_in_flight is False


def test_post_compress_warning_error_releases_lease_and_marker(tmp_path: Path) -> None:
    """Any host callback failure after rewrite must not strand ownership."""
    db = open_cli_session_store(db_path=tmp_path / "state.db")
    parent_sid = "POST_COMPRESS_WARNING_ERROR"
    db.sessions.create(parent_sid, source="discord")
    agent = _build_agent_with_db(db, parent_sid)
    agent.context_compressor._last_summary_error = "summary unavailable"
    agent._emit_warning = MagicMock(side_effect=RuntimeError("warning failed"))
    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]

    with pytest.raises(RuntimeError, match="warning failed"):
        agent._compress_context(messages, "sys", approx_tokens=120_000)

    assert db.compression_leases.holder(parent_sid) is None
    assert agent._compression_in_flight is False
    assert agent.session_id == parent_sid


def test_post_compress_todo_error_releases_lease_and_marker(tmp_path: Path) -> None:
    db = open_cli_session_store(db_path=tmp_path / "state.db")
    parent_sid = "POST_COMPRESS_TODO_ERROR"
    db.sessions.create(parent_sid, source="discord")
    agent = _build_agent_with_db(db, parent_sid)
    agent._todo_store.format_for_injection = MagicMock(
        side_effect=RuntimeError("todo failed")
    )
    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]

    with pytest.raises(RuntimeError, match="todo failed"):
        agent._compress_context(messages, "sys", approx_tokens=120_000)

    assert db.compression_leases.holder(parent_sid) is None
    assert agent._compression_in_flight is False
    assert agent.session_id == parent_sid


def test_status_callback_error_never_sets_compression_marker(tmp_path: Path) -> None:
    db = open_cli_session_store(db_path=tmp_path / "state.db")
    parent_sid = "STATUS_CALLBACK_ERROR"
    db.sessions.create(parent_sid, source="discord")
    agent = _build_agent_with_db(db, parent_sid)
    agent._emit_status = MagicMock(side_effect=RuntimeError("status failed"))
    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]

    with pytest.raises(RuntimeError, match="status failed"):
        agent._compress_context(messages, "sys", approx_tokens=120_000)

    assert db.compression_leases.holder(parent_sid) is None
    assert agent._compression_in_flight is False
    agent.context_compressor.compress.assert_not_called()


def test_review_fork_disables_compression_to_prevent_stale_parent_fork() -> None:
    """The background-review fork must set ``compression_enabled = False``
    so it can never compress the parent it shares a session_id with
    (issue #38727).

    The per-session compression lock only serialises a SAME-WINDOW concurrent
    race. It does NOT stop a stale parent from being compressed again in a
    LATER turn: if ``review_agent`` had won the race, its new child session is
    never adopted by the gateway (the fork is single-lifecycle and dies right
    after one ``run_conversation``), so the foreground path would start the
    next turn from the stale parent and compress it AGAIN — leaving the same
    parent with two sibling children.

    The fix makes the review fork never trigger compression at all. Both
    compression trigger sites in ``agent/conversation_loop.py`` gate on
    ``agent.compression_enabled`` BEFORE calling ``_compress_context``:
      • preflight (``if agent.compression_enabled and len(messages) > ...``)
      • mid-loop  (``if agent.compression_enabled and _compressor.should_compress(...)``)
    so a fork with the flag cleared never reaches the rotation path.

    This test pins the contract at the source: ``_run_review_in_thread``
    must set ``review_agent.compression_enabled = False`` on the fork it
    builds. It calls the real worker synchronously with
    ``AIAgent.run_conversation`` patched (so no LLM call happens) and
    captures the constructed review agent to assert the flag.
    """
    import tempfile

    import agent.background_review as br

    captured = {}

    def _fake_run_conversation(self, *_a, **_k):
        captured["compression_enabled"] = self.compression_enabled
        captured["session_id"] = self.session_id
        return {"final_response": "", "messages": []}

    parent_sid = "REVIEW_FORK_FLAG_TEST"

    with tempfile.TemporaryDirectory() as td:
        db = open_cli_session_store(db_path=Path(td) / "state.db")
        db.sessions.create(parent_sid, source="discord")
        parent = _build_agent_with_db(db, parent_sid)

        # The worker does a local ``from run_agent import AIAgent``; patching
        # the class method covers that import path.
        from run_agent import AIAgent

        with patch.object(AIAgent, "run_conversation", _fake_run_conversation):
            br._run_review_in_thread(
                parent,
                [{"role": "user", "content": "hi"}],
                "review this conversation",
            )

    assert captured, (
        "_run_review_in_thread never reached run_conversation — the spawn path "
        "changed; update this test to capture the review AIAgent."
    )
    assert captured["session_id"] == parent_sid, (
        "Review fork should inherit the parent's session_id (shared id is the "
        "whole reason compression must be disabled)."
    )
    assert captured["compression_enabled"] is False, (
        "FIX REGRESSION: background-review fork did NOT disable compression. "
        "It shares the parent's session_id, so an enabled fork can rotate the "
        "parent into an orphan child (issue #38727). The trigger gates in "
        "conversation_loop.py only short-circuit when compression_enabled is "
        "False — this flag MUST be cleared on the review fork."
    )
