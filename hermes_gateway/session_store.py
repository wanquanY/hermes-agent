"""Gateway session-key index and transcript access."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from channels.session_identity import SessionSource, build_session_key
from hermes_agent.read_models.message_history import MessageHistoryReadModel
from hermes_agent.repositories.message_repo import MessageRepository
from hermes_agent.repositories.session_repo import SessionFilter, SessionRepo, SessionSpec
from hermes_agent.storage.session_repository_db import connect_session_repository_db
from hermes_gateway.config import GatewayConfig
from hermes_gateway.session_entry import SessionEntry
from utils import atomic_replace

logger = logging.getLogger(__name__)


def _now() -> datetime:
    """Return the current local time."""
    return datetime.now()


class SessionStore:
    """
    Manages session storage and retrieval.
    
    Session metadata is owned by SessionRepo. The JSON file remains only the
    gateway's session-key index, and transcript persistence is isolated to the
    transcript methods until the P2 message slice moves that owner as well.
    """
    
    def __init__(self, sessions_dir: Path, config: GatewayConfig,
                 has_active_processes_fn=None,
                 session_repo: Optional[SessionRepo] = None,
                 storage_conn: sqlite3.Connection | None = None):
        self.sessions_dir = sessions_dir
        self.config = config
        self._entries: Dict[str, SessionEntry] = {}
        self._loaded = False
        self._lock = threading.Lock()
        self._has_active_processes_fn = has_active_processes_fn
        self._storage_conn: sqlite3.Connection | None = storage_conn
        if self._storage_conn is None and session_repo is None:
            self._storage_conn = connect_session_repository_db()
        if session_repo is not None:
            self._session_repo = session_repo
        else:
            from hermes_agent.repositories.session_repo import SessionRepoImpl

            assert self._storage_conn is not None
            self._session_repo = SessionRepoImpl(self._storage_conn)
        self._message_repo = (
            MessageRepository(self._storage_conn)
            if self._storage_conn is not None
            else None
        )
        self._message_history = (
            MessageHistoryReadModel(self._storage_conn)
            if self._storage_conn is not None
            else None
        )
    
    def _ensure_loaded(self) -> None:
        """Load sessions index from disk if not already loaded."""
        with self._lock:
            self._ensure_loaded_locked()

    def _ensure_loaded_locked(self) -> None:
        """Load sessions index from disk. Must be called with self._lock held."""
        if self._loaded:
            return

        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_file = self.sessions_dir / "sessions.json"

        if sessions_file.exists():
            try:
                with open(sessions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for key, entry_data in data.items():
                        try:
                            self._entries[key] = SessionEntry.from_dict(entry_data)
                        except (ValueError, KeyError):
                            # Skip entries with unknown/removed platform values
                            continue
            except Exception as e:
                print(f"[gateway] Warning: Failed to load sessions: {e}")

        self._loaded = True
    
    def _save(self) -> None:
        """Save sessions index to disk (kept for session key -> ID mapping)."""
        import tempfile
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_file = self.sessions_dir / "sessions.json"

        data = {key: entry.to_dict() for key, entry in self._entries.items()}
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.sessions_dir), suffix=".tmp", prefix=".sessions_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            atomic_replace(tmp_path, sessions_file)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError as e:
                logger.debug("Could not remove temp file %s: %s", tmp_path, e)
            raise
    
    def _generate_session_key(self, source: SessionSource) -> str:
        """Generate a session key from a source."""
        return build_session_key(
            source,
            group_sessions_per_user=getattr(self.config, "group_sessions_per_user", True),
            thread_sessions_per_user=getattr(self.config, "thread_sessions_per_user", False),
        )
    
    def _is_session_expired(self, entry: SessionEntry) -> bool:
        """Check if a session has expired based on its reset policy.
        
        Works from the entry alone — no SessionSource needed.
        Used by the background expiry watcher to proactively flush memories.
        Sessions with active background processes are never considered expired.
        """
        if self._has_active_processes_fn:
            if self._has_active_processes_fn(entry.session_key):
                return False

        policy = self.config.get_reset_policy(
            platform=entry.platform,
            session_type=entry.chat_type,
        )

        if policy.mode == "none":
            return False

        now = _now()

        if policy.mode in {"idle", "both"}:
            idle_deadline = entry.updated_at + timedelta(minutes=policy.idle_minutes)
            if now > idle_deadline:
                return True

        if policy.mode in {"daily", "both"}:
            today_reset = now.replace(
                hour=policy.at_hour,
                minute=0, second=0, microsecond=0,
            )
            if now.hour < policy.at_hour:
                today_reset -= timedelta(days=1)
            if entry.updated_at < today_reset:
                return True

        return False

    def _should_reset(self, entry: SessionEntry, source: SessionSource) -> Optional[str]:
        """
        Check if a session should be reset based on policy.
        
        Returns the reset reason ("idle" or "daily") if a reset is needed,
        or None if the session is still valid.
        
        Sessions with active background processes are never reset.
        """
        if self._has_active_processes_fn:
            session_key = self._generate_session_key(source)
            if self._has_active_processes_fn(session_key):
                return None

        policy = self.config.get_reset_policy(
            platform=source.platform,
            session_type=source.chat_type
        )
        
        if policy.mode == "none":
            return None
        
        now = _now()
        
        if policy.mode in {"idle", "both"}:
            idle_deadline = entry.updated_at + timedelta(minutes=policy.idle_minutes)
            if now > idle_deadline:
                return "idle"
        
        if policy.mode in {"daily", "both"}:
            today_reset = now.replace(
                hour=policy.at_hour, 
                minute=0, 
                second=0, 
                microsecond=0
            )
            if now.hour < policy.at_hour:
                today_reset -= timedelta(days=1)
            
            if entry.updated_at < today_reset:
                return "daily"
        
        return None
    
    def has_any_sessions(self) -> bool:
        """Check if any sessions have ever been created (across all platforms).

        Uses the SessionRepo as the source of truth because it preserves
        historical session records (ended sessions still count).  The in-memory
        ``_entries`` dict replaces entries on reset, so ``len(_entries)`` would
        stay at 1 for single-platform users — which is the bug this fixes.

        The current session is already in the DB by the time this is called
        (get_or_create_session runs first), so we check ``> 1``.
        """
        try:
            return len(list(self._session_repo.list(SessionFilter(include_ended=True, limit=2)))) > 1
        except Exception:
            logger.debug("Session repository count check failed", exc_info=True)
        # Fallback: check if sessions.json was loaded with existing data.
        # This covers the rare case where the DB is unavailable.
        with self._lock:
            self._ensure_loaded_locked()
            return len(self._entries) > 1

    def get_or_create_session(
        self,
        source: SessionSource,
        force_new: bool = False
    ) -> SessionEntry:
        """
        Get an existing session or create a new one.

        Evaluates reset policy to determine if the existing session is stale.
        Creates a session record in SQLite when a new session starts.
        """
        session_key = self._generate_session_key(source)
        now = _now()

        # Repository calls are made outside the lock to avoid holding it during I/O.
        # All _entries / _loaded mutations are protected by self._lock.
        repo_end_session_id = None
        repo_create_spec = None

        with self._lock:
            self._ensure_loaded_locked()

            if session_key in self._entries and not force_new:
                entry = self._entries[session_key]

                # Auto-reset sessions marked as suspended (e.g. after /stop
                # broke a stuck loop — #7536).  ``suspended`` is the hard
                # forced-wipe signal and always wins over ``resume_pending``,
                # so repeated interrupted restarts that escalate via the
                # existing ``.restart_failure_counts`` stuck-loop counter
                # still converge to a clean slate.
                if entry.suspended:
                    reset_reason = "suspended"
                elif entry.resume_pending:
                    # Restart-interrupted session: preserve the session_id
                    # and return the existing entry so the transcript
                    # reloads intact.  ``resume_pending`` is cleared after
                    # the NEXT successful turn completes (not here), which
                    # means a re-interrupted retry keeps trying — the
                    # stuck-loop counter handles terminal escalation.
                    entry.updated_at = now
                    self._save()
                    return entry
                else:
                    reset_reason = self._should_reset(entry, source)
                if not reset_reason:
                    entry.updated_at = now
                    self._save()
                    return entry
                else:
                    # Session is being auto-reset.
                    was_auto_reset = True
                    auto_reset_reason = reset_reason
                    # Track whether the expired session had any real conversation
                    reset_had_activity = entry.total_tokens > 0
                    repo_end_session_id = entry.session_id
            else:
                was_auto_reset = False
                auto_reset_reason = None
                reset_had_activity = False

            # Create new session
            session_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

            entry = SessionEntry(
                session_key=session_key,
                session_id=session_id,
                created_at=now,
                updated_at=now,
                origin=source,
                display_name=source.chat_name,
                platform=source.platform,
                chat_type=source.chat_type,
                was_auto_reset=was_auto_reset,
                auto_reset_reason=auto_reset_reason,
                reset_had_activity=reset_had_activity,
            )

            self._entries[session_key] = entry
            self._save()
            repo_create_spec = SessionSpec(
                session_id=session_id,
                source=source.platform.value,
                title=source.chat_name or "",
                display_title=source.chat_name or "",
            )

        if repo_end_session_id:
            self._session_repo.close(repo_end_session_id, "session_reset")

        if repo_create_spec:
            self._session_repo.create(repo_create_spec)

        return entry

    def update_session(
        self,
        session_key: str,
        last_prompt_tokens: int = None,
    ) -> None:
        """Update lightweight session metadata after an interaction."""
        with self._lock:
            self._ensure_loaded_locked()

            if session_key in self._entries:
                entry = self._entries[session_key]
                entry.updated_at = _now()
                if last_prompt_tokens is not None:
                    entry.last_prompt_tokens = last_prompt_tokens
                self._save()

    def suspend_session(self, session_key: str) -> bool:
        """Mark a session as suspended so it auto-resets on next access.

        Used by ``/stop`` to prevent stuck sessions from being resumed
        after a gateway restart (#7536).  Returns True if the session
        existed and was marked.
        """
        with self._lock:
            self._ensure_loaded_locked()
            if session_key in self._entries:
                self._entries[session_key].suspended = True
                self._save()
                return True
        return False

    def mark_resume_pending(
        self,
        session_key: str,
        reason: str = "restart_timeout",
    ) -> bool:
        """Mark a session as resumable after a restart interruption.

        Unlike ``suspend_session()``, this preserves the existing
        ``session_id`` and the transcript.  The next call to
        ``get_or_create_session()`` for this key returns the same entry
        so the user auto-resumes on the same conversation lane.

        Returns True if the session existed and was marked.
        """
        with self._lock:
            self._ensure_loaded_locked()
            if session_key in self._entries:
                entry = self._entries[session_key]
                # Never override an explicit ``suspended`` — that is a hard
                # forced-wipe signal (from /stop or stuck-loop escalation).
                if entry.suspended:
                    return False
                entry.resume_pending = True
                entry.resume_reason = reason
                entry.last_resume_marked_at = _now()
                self._save()
                return True
        return False

    def get_entry(self, session_key: str) -> Optional[SessionEntry]:
        """Return the current SessionEntry for a session key without mutating it."""
        if not session_key:
            return None
        with self._lock:
            self._ensure_loaded_locked()
            return self._entries.get(session_key)

    def update_entry_session_id(self, session_key: str, session_id: str) -> bool:
        """Update the transcript session id for a known gateway session key."""
        if not session_key or not session_id:
            return False
        with self._lock:
            self._ensure_loaded_locked()
            entry = self._entries.get(session_key)
            if entry is None:
                return False
            if entry.session_id == session_id:
                return True
            entry.session_id = session_id
            self._save()
            return True

    def clear_resume_pending(self, session_key: str) -> bool:
        """Clear the resume-pending flag after a successful resumed turn.

        Called from the gateway after ``run_conversation()`` returns a
        final response for a session that had ``resume_pending=True``,
        signalling that recovery succeeded.

        Returns True if a flag was cleared.
        """
        with self._lock:
            self._ensure_loaded_locked()
            entry = self._entries.get(session_key)
            if entry is None or not entry.resume_pending:
                return False
            entry.resume_pending = False
            entry.resume_reason = None
            entry.last_resume_marked_at = None
            self._save()
            return True

    def prune_old_entries(self, max_age_days: int) -> int:
        """Drop SessionEntry records older than max_age_days.

        Pruning is based on ``updated_at`` (last activity), not ``created_at``.
        A session that's been active within the window is kept regardless of
        how old it is.  Entries marked ``suspended`` are kept — the user
        explicitly paused them for later resume.  Entries held by an active
        process (via has_active_processes_fn) are also kept so long-running
        background work isn't orphaned.

        Pruning is functionally identical to a natural reset-policy expiry:
        the transcript in SQLite stays, but the session_key → session_id
        mapping is dropped and the user starts a fresh session on return.

        ``max_age_days <= 0`` disables pruning; returns 0 immediately.
        Returns the number of entries removed.
        """
        if max_age_days is None or max_age_days <= 0:
            return 0
        from datetime import timedelta

        cutoff = _now() - timedelta(days=max_age_days)
        removed_keys: list[str] = []

        with self._lock:
            self._ensure_loaded_locked()
            for key, entry in list(self._entries.items()):
                if entry.suspended:
                    continue
                # Never prune sessions with an active background process
                # attached — the user may still be waiting on output.
                # The callback is keyed by session_key (see process_registry.
                # has_active_for_session); passing session_id here used to
                # never match, so active sessions got pruned anyway.
                if self._has_active_processes_fn is not None:
                    try:
                        if self._has_active_processes_fn(entry.session_key):
                            continue
                    except Exception as exc:
                        logger.debug(
                            "has_active_processes_fn raised during prune for %s: %s",
                            entry.session_key, exc,
                        )
                if entry.updated_at < cutoff:
                    removed_keys.append(key)
            for key in removed_keys:
                self._entries.pop(key, None)
            if removed_keys:
                self._save()

        if removed_keys:
            logger.info(
                "SessionStore pruned %d entries older than %d days",
                len(removed_keys), max_age_days,
            )
        return len(removed_keys)

    def suspend_recently_active(self, max_age_seconds: int = 120) -> int:
        """Mark recently-active sessions as resumable after an unexpected exit.

        Called on gateway startup after a crash or fast restart to preserve
        in-flight sessions instead of destroying their conversation history
        (#7536).  Only marks sessions updated within *max_age_seconds* to
        avoid touching long-idle sessions.  Sets ``resume_pending=True`` so
        the next incoming message on the same session_key auto-resumes from
        the existing transcript.

        Entries already flagged ``resume_pending=True`` are skipped.  Entries
        explicitly ``suspended=True`` (from /stop or stuck-loop escalation)
        are also skipped.  Terminal escalation for genuinely stuck sessions
        is still handled by the existing ``.restart_failure_counts`` counter
        (threshold 3), which runs after this method and sets ``suspended=True``.

        Returns the number of sessions marked resumable.
        """
        from datetime import timedelta

        cutoff = _now() - timedelta(seconds=max_age_seconds)
        count = 0
        with self._lock:
            self._ensure_loaded_locked()
            for entry in self._entries.values():
                if entry.resume_pending:
                    continue
                if not entry.suspended and entry.updated_at >= cutoff:
                    entry.resume_pending = True
                    entry.resume_reason = "restart_interrupted"
                    entry.last_resume_marked_at = _now()
                    count += 1
            if count:
                self._save()
        return count

    def reset_session(self, session_key: str, display_name: Optional[str] = None) -> Optional[SessionEntry]:
        """Force reset a session, creating a new session ID."""
        repo_end_session_id = None
        repo_create_spec = None
        new_entry = None

        with self._lock:
            self._ensure_loaded_locked()

            if session_key not in self._entries:
                return None

            old_entry = self._entries[session_key]
            repo_end_session_id = old_entry.session_id

            now = _now()
            session_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

            new_entry = SessionEntry(
                session_key=session_key,
                session_id=session_id,
                created_at=now,
                updated_at=now,
                origin=old_entry.origin,
                display_name=display_name if display_name is not None else old_entry.display_name,
                platform=old_entry.platform,
                chat_type=old_entry.chat_type,
                is_fresh_reset=True,
            )

            self._entries[session_key] = new_entry
            self._save()
            repo_create_spec = SessionSpec(
                session_id=session_id,
                source=old_entry.platform.value if old_entry.platform else "unknown",
                title=new_entry.display_name or "",
                display_title=new_entry.display_name or "",
            )

        if repo_end_session_id:
            self._session_repo.close(repo_end_session_id, "session_reset")

        if repo_create_spec:
            self._session_repo.create(repo_create_spec)

        return new_entry

    def switch_session(self, session_key: str, target_session_id: str) -> Optional[SessionEntry]:
        """Switch a session key to point at an existing session ID.

        Used by ``/resume`` to restore a previously-named session.
        Ends the current session in SQLite (like reset), but instead of
        generating a fresh session ID, re-uses ``target_session_id`` so the
        old transcript is loaded on the next message. If the target session was
        previously ended, re-open it so gateway resume semantics match the CLI.
        """
        repo_end_session_id = None
        new_entry = None

        with self._lock:
            self._ensure_loaded_locked()

            if session_key not in self._entries:
                return None

            old_entry = self._entries[session_key]

            # Don't switch if already on that session
            if old_entry.session_id == target_session_id:
                return old_entry

            repo_end_session_id = old_entry.session_id

            now = _now()
            new_entry = SessionEntry(
                session_key=session_key,
                session_id=target_session_id,
                created_at=now,
                updated_at=now,
                origin=old_entry.origin,
                display_name=old_entry.display_name,
                platform=old_entry.platform,
                chat_type=old_entry.chat_type,
            )

            self._entries[session_key] = new_entry
            self._save()

        if repo_end_session_id:
            self._session_repo.close(repo_end_session_id, "session_switch")
        self._session_repo.reopen(target_session_id)

        return new_entry

    def list_sessions(self, active_minutes: Optional[int] = None) -> List[SessionEntry]:
        """List all sessions, optionally filtered by activity."""
        with self._lock:
            self._ensure_loaded_locked()
            entries = list(self._entries.values())

        if active_minutes is not None:
            cutoff = _now() - timedelta(minutes=active_minutes)
            entries = [e for e in entries if e.updated_at >= cutoff]

        entries.sort(key=lambda e: e.updated_at, reverse=True)

        return entries
    
    def append_to_transcript(self, session_id: str, message: Dict[str, Any], skip_db: bool = False) -> None:
        """Append a message to a session's transcript (SQLite).

        Args:
            skip_db: When True, skip the SQLite write. Used when the agent
                     already persisted messages to SQLite via its own
                     _flush_messages_to_session_db(), preventing the
                     duplicate-write bug (#860).
        """
        if skip_db or self._message_repo is None:
            return
        try:
            self._message_repo.append_conversation_message(session_id, message)
        except Exception as e:
            logger.debug("Transcript repository operation failed: %s", e)
    
    def rewrite_transcript(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Replace the entire transcript for a session with new messages.

        Used by /retry, /undo, and /compress to persist modified conversation
        history. state.db is the canonical store.
        """
        if self._message_repo is None:
            return
        try:
            self._message_repo.replace_conversation(session_id, messages)
        except Exception as e:
            logger.debug("Failed to rewrite transcript in repository: %s", e)

    def load_transcript(self, session_id: str) -> List[Dict[str, Any]]:
        """Load all messages from a session's transcript.

        state.db is the canonical store. The legacy JSONL fallback was removed
        in spec 002 — pre-DB sessions on existing disks have already been
        migrated (their DB row holds the full message history).
        """
        if self._message_history is None:
            return []
        try:
            return self._message_history.all_as_conversation(session_id)
        except Exception as e:
            logger.debug("Could not load messages from repository: %s", e)
            return []
