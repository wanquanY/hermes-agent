"""Tests for ``agent.conversation_loop._restore_or_build_system_prompt``.

Validates the gateway DB-roundtrip path that keeps the system prompt
byte-stable across turns (fresh AIAgent → must restore from session DB
instead of rebuilding).  Covers:

  * Successful restore from a stored prompt (present row).
  * Legitimate first-turn build (no history).
  * Silent-failure recovery paths:
      - DB read raises → WARNING + fresh build
      - Row has system_prompt=NULL → WARNING + fresh build
      - Row has system_prompt="" → WARNING + fresh build
      - DB write fails → WARNING (subsequent turns will miss cache)
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.conversation_loop import (
    _restore_or_build_system_prompt,
    _system_prompt_execution_scope_key,
)


def _make_agent(session_db=None, prebuilt_prompt: str = "BUILT_PROMPT"):
    """Construct the minimal agent fake the helper needs."""
    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = "test-session-id"
    agent.model = "test-model"
    agent.provider = "test-provider"
    agent.valid_tool_names = set()
    agent._tool_use_enforcement = "auto"
    agent.platform = "cli"
    agent._session_db = session_db
    agent.run_context = None
    agent._run_context = None
    agent._build_system_prompt = MagicMock(return_value=prebuilt_prompt)
    if session_db is not None:
        # Production SessionDB exposes ordinary prompt persistence through the
        # ``sessions`` component; retain the older assertions as call probes.
        session_db.sessions.get.side_effect = session_db.get_session
        session_db.sessions.update_system_prompt.side_effect = (
            session_db.update_system_prompt
        )
    return agent


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestStoredPromptReuse:
    def test_present_row_is_reused_verbatim(self, caplog):
        """Continuing session with a stored prompt → reuse byte-for-byte."""
        stored = "Stored prompt from turn 1 — byte-identical reuse"
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": stored}
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        assert agent._cached_system_prompt == stored
        agent._build_system_prompt.assert_not_called()
        db.update_system_prompt.assert_not_called()
        # No warnings on the happy path
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_legacy_hermes_brand_prompt_is_rebuilt(self, caplog):
        """Old cached prompts must not keep leaking Hermes identity."""
        stored = (
            "You are Hermes Agent.\n\n"
            "Active Hermes profile: default.\n"
            "Conversation started: Sunday, May 17, 2026\n"
        )
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": stored}
        agent = _make_agent(session_db=db, prebuilt_prompt="You are Dovie.\nConversation started: today")

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        assert agent._cached_system_prompt == "You are Dovie.\nConversation started: today"
        agent._build_system_prompt.assert_called_once_with(None)
        db.update_system_prompt.assert_called_once_with(agent.session_id, agent._cached_system_prompt)
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("legacy_dovie_branding" in message for message in warnings)

    def test_present_row_with_unicode_preserved(self):
        """Non-ASCII bytes in the stored prompt are not mangled."""
        stored = "Stored prompt with unicode: ☤ ⚗ ◆ — and emoji 🦊"
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": stored}
        agent = _make_agent(session_db=db)

        _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])
        assert agent._cached_system_prompt == stored


# ---------------------------------------------------------------------------
# Legitimate fresh-build paths (no history, no DB)
# ---------------------------------------------------------------------------


class TestLegitimateFreshBuild:
    def test_no_history_skips_db_and_builds_fresh(self, caplog):
        """First turn with empty history → build fresh, don't touch the DB."""
        db = MagicMock()
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [])

        # No history → DB read skipped entirely
        db.get_session.assert_not_called()
        agent._build_system_prompt.assert_called_once_with(None)
        assert agent._cached_system_prompt == "BUILT_PROMPT"
        # Persisted to DB
        db.update_system_prompt.assert_called_once_with(agent.session_id, "BUILT_PROMPT")
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_no_db_skips_persistence(self):
        """When session DB is None, build and skip persistence silently."""
        agent = _make_agent(session_db=None)
        _restore_or_build_system_prompt(agent, None, [])
        agent._build_system_prompt.assert_called_once()
        assert agent._cached_system_prompt == "BUILT_PROMPT"


# ---------------------------------------------------------------------------
# Silent-failure recovery — these are the new A/B logging paths
# ---------------------------------------------------------------------------


class TestSilentFailureWarnings:
    def test_db_read_exception_warns_and_rebuilds(self, caplog):
        """DB read raising → WARNING + fall through to fresh build."""
        db = MagicMock()
        db.get_session.side_effect = RuntimeError("disk full")
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        # Built fresh
        agent._build_system_prompt.assert_called_once()
        assert agent._cached_system_prompt == "BUILT_PROMPT"
        # Loud warning about the read failure
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("system-prompt restore failed" in r.getMessage() for r in warnings), \
            f"Expected a get_session warning, got: {[r.getMessage() for r in warnings]}"
        assert any("disk full" in r.getMessage() for r in warnings)

    def test_null_system_prompt_warns_about_unusable_stored_state(self, caplog):
        """Row exists but system_prompt is NULL → WARNING + fresh build."""
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": None}
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        agent._build_system_prompt.assert_called_once()
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("is null" in m and "rebuilding" in m for m in warnings), \
            f"Expected null-stored-prompt warning, got: {warnings}"

    def test_empty_system_prompt_warns_about_silent_persistence_bug(self, caplog):
        """Row exists but system_prompt is '' → WARNING about silent write bug."""
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": ""}
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        agent._build_system_prompt.assert_called_once()
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("is empty" in m and "rebuilding" in m for m in warnings), \
            f"Expected empty-stored-prompt warning, got: {warnings}"

    def test_db_write_failure_warns_loudly(self, caplog):
        """update_system_prompt raising → WARNING (was DEBUG before)."""
        db = MagicMock()
        # No prior row (first turn)
        db.get_session.return_value = None
        db.update_system_prompt.side_effect = RuntimeError("database is locked")
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            _restore_or_build_system_prompt(agent, None, [])

        # Built and assigned the cache anyway
        agent._build_system_prompt.assert_called_once()
        assert agent._cached_system_prompt == "BUILT_PROMPT"
        # Warning surfaced
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "system-prompt persistence failed" in m and "database is locked" in m
            for m in warnings
        ), f"Expected write-failure warning, got: {warnings}"

    def test_no_history_with_null_row_does_not_warn(self, caplog):
        """First turn (no history) hitting a null row is not surprising — no warn."""
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": None}
        agent = _make_agent(session_db=db)

        with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
            # Empty history → DB read is skipped entirely
            _restore_or_build_system_prompt(agent, None, [])

        db.get_session.assert_not_called()
        # No "rebuilding from scratch" warning because history is empty
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("rebuilding" in m for m in warnings)


# ---------------------------------------------------------------------------
# Byte-stability invariant
# ---------------------------------------------------------------------------


class TestPromptStabilityInvariant:
    def test_restored_prompt_is_byte_identical_to_stored(self):
        """The restored prompt must equal the stored bytes exactly — no
        normalization, trimming, or concat that could shift the prefix.

        This is the core invariant: any byte-level change at this point
        invalidates KV cache on every prefix-cache backend.
        """
        stored = (
            "You are Dovie.\n"
            "\n"
            "Conversation started: Sunday, May 17, 2026\n"
            "Session ID: 20260517_153500_abc123\n"
        )
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": stored}
        agent = _make_agent(session_db=db)

        _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        # Identity check — must be the same object reference for maximum
        # confidence we're not slicing/copying/normalizing.
        assert agent._cached_system_prompt == stored
        # Byte-level check
        assert agent._cached_system_prompt.encode("utf-8") == stored.encode("utf-8")


class TestExecutionScopedPromptReuse:
    def test_scoped_prompt_cache_key_changes_with_prompt_tool_surface(self):
        agent = _make_agent()
        agent.run_context = SimpleNamespace(
            conversation_session_id=agent.session_id,
            execution_scope_key="member-chat:test-session-id:frontend",
        )

        without_tools = _system_prompt_execution_scope_key(agent)
        agent.valid_tool_names = {"team_mission_status"}
        with_tools = _system_prompt_execution_scope_key(agent)

        assert without_tools.startswith(
            "member-chat:test-session-id:frontend:prompt-v1:"
        )
        assert with_tools.startswith(
            "member-chat:test-session-id:frontend:prompt-v1:"
        )
        assert with_tools != without_tools

    def test_scoped_prompt_cache_key_changes_with_profile_version(self):
        agent = _make_agent()
        agent.run_context = SimpleNamespace(
            conversation_session_id=agent.session_id,
            execution_scope_key="team:test-session-id:leader-conversation",
            profile_id="leader-profile",
            profile_version_id="version-1",
            execution_home="/tmp/leader-profile",
        )

        version_one = _system_prompt_execution_scope_key(agent)
        agent.run_context.profile_version_id = "version-2"
        version_two = _system_prompt_execution_scope_key(agent)

        assert version_one != version_two

    def test_scoped_run_reuses_scoped_prompt_not_transcript_prompt(self):
        db = MagicMock()
        db.get_scoped_system_prompt.return_value = "FRONTEND_SCOPED_PROMPT"
        agent = _make_agent(session_db=db)
        agent.run_context = SimpleNamespace(
            conversation_session_id=agent.session_id,
            execution_scope_key="member-chat:test-session-id:frontend",
        )
        prompt_scope_key = _system_prompt_execution_scope_key(agent)

        _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        assert agent._cached_system_prompt == "FRONTEND_SCOPED_PROMPT"
        db.get_scoped_system_prompt.assert_called_once_with(
            agent.session_id,
            prompt_scope_key,
        )
        db.get_session.assert_not_called()
        agent._build_system_prompt.assert_not_called()
        db.update_system_prompt.assert_not_called()

    def test_scoped_run_writes_scoped_prompt_without_overwriting_transcript_prompt(self):
        db = MagicMock()
        db.get_scoped_system_prompt.return_value = None
        agent = _make_agent(session_db=db)
        agent.run_context = SimpleNamespace(
            conversation_session_id=agent.session_id,
            execution_scope_key="member-chat:test-session-id:frontend",
        )
        prompt_scope_key = _system_prompt_execution_scope_key(agent)

        _restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "hi"}])

        assert agent._cached_system_prompt == "BUILT_PROMPT"
        agent._build_system_prompt.assert_called_once_with(None)
        db.update_scoped_system_prompt.assert_called_once_with(
            agent.session_id,
            prompt_scope_key,
            "BUILT_PROMPT",
        )
        db.update_system_prompt.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
