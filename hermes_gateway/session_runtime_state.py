"""Per-session runtime state owned by the gateway runner."""

from __future__ import annotations

import logging
from typing import Any, Optional

from hermes_gateway.runtime_status_writer import runtime_status_for

logger = logging.getLogger(__name__)


class GatewaySessionRuntimeStateMixin:
    def _release_running_agent_state(
        self,
        session_key: str,
        *,
        run_generation: Optional[int] = None,
    ) -> bool:
        """Pop all per-running-agent state entries for ``session_key``."""
        if not session_key:
            return False
        if run_generation is not None and not self._is_session_run_current(
            session_key, run_generation
        ):
            return False
        self._running_agents.pop(session_key, None)
        self._running_agents_ts.pop(session_key, None)
        if hasattr(self, "_busy_ack_ts"):
            self._busy_ack_ts.pop(session_key, None)
        runtime_status_for(self).persist_active_agents()
        return True

    def _clear_session_boundary_security_state(self, session_key: str) -> None:
        """Clear per-session control state that must not survive a boundary switch."""
        if not session_key:
            return

        pending_skills_reload_notes = getattr(
            self, "_pending_skills_reload_notes", None
        )
        if isinstance(pending_skills_reload_notes, dict):
            pending_skills_reload_notes.pop(session_key, None)

        pending_approvals = getattr(self, "_pending_approvals", None)
        if isinstance(pending_approvals, dict):
            pending_approvals.pop(session_key, None)

        update_prompt_pending = getattr(self, "_update_prompt_pending", None)
        if isinstance(update_prompt_pending, dict):
            update_prompt_pending.pop(session_key, None)

        try:
            from tools import slash_confirm as slash_confirm
        except Exception:
            slash_confirm = None
        if slash_confirm is not None:
            try:
                slash_confirm.clear(session_key)
            except Exception as exc:
                logger.debug(
                    "Failed to clear slash-confirm state for session boundary %s: %s",
                    session_key,
                    exc,
                )

        try:
            from tools.approval import clear_session as clear_approval_session
        except Exception:
            return

        try:
            clear_approval_session(session_key)
        except Exception as exc:
            logger.debug(
                "Failed to clear approval state for session boundary %s: %s",
                session_key,
                exc,
            )

    def _begin_session_run_generation(self, session_key: str) -> int:
        """Claim a fresh run generation token for ``session_key``."""
        if not session_key:
            return 0
        generations = self.__dict__.get("_session_run_generation")
        if generations is None:
            generations = {}
            self._session_run_generation = generations
        next_generation = int(generations.get(session_key, 0)) + 1
        generations[session_key] = next_generation
        return next_generation

    def _invalidate_session_run_generation(self, session_key: str, *, reason: str = "") -> int:
        """Invalidate any in-flight run token for ``session_key``."""
        generation = self._begin_session_run_generation(session_key)
        if reason:
            logger.info(
                "Invalidated run generation for %s → %d (%s)",
                session_key,
                generation,
                reason,
            )
        return generation

    def _is_session_run_current(self, session_key: str, generation: int) -> bool:
        """Return True when ``generation`` is still current for ``session_key``."""
        if not session_key:
            return True
        generations = self.__dict__.get("_session_run_generation") or {}
        return int(generations.get(session_key, 0)) == int(generation)

    def _bind_adapter_run_generation(
        self,
        adapter: Any,
        session_key: str,
        generation: int | None,
    ) -> None:
        """Bind a gateway run generation to the adapter's active-session event."""
        if not adapter or not session_key or generation is None:
            return
        try:
            interrupt_event = getattr(adapter, "_active_sessions", {}).get(session_key)
            if interrupt_event is not None:
                setattr(interrupt_event, "_hermes_run_generation", int(generation))
        except Exception:
            pass
