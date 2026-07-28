"""User-interaction callbacks installed on a gateway agent turn."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from typing import Any

from agent.async_utils import safe_schedule_threadsafe

logger = logging.getLogger(__name__)


class AgentInteractionCallbacks:
    """Owns clarify, approval, and background-review callbacks for one turn."""

    def __init__(
        self,
        *,
        status_adapter,
        status_chat_id: str,
        status_thread_metadata: dict[str, Any] | None,
        session_key: str | None,
        run_generation: int | None,
        loop,
        run_still_current: Callable[[], bool],
        redact_approval_command: Callable[[str | None], str],
    ) -> None:
        self._status_adapter = status_adapter
        self._status_chat_id = status_chat_id
        self._status_thread_metadata = status_thread_metadata
        self._session_key = session_key or ""
        self._run_generation = run_generation
        self._loop = loop
        self._run_still_current = run_still_current
        self._redact_approval_command = redact_approval_command
        self._background_release = threading.Event()
        self._background_pending: list[str] = []
        self._background_lock = threading.Lock()

    def install_on(self, agent) -> None:
        agent.background_review_callback = self.send_background_review
        agent.clarify_callback = self.clarify
        if self._status_adapter and self._session_key:
            self._status_adapter.register_post_delivery_callback(
                self._session_key,
                self.release_background_reviews,
                generation=self._run_generation,
            )

    def activate_approval(self):
        from tools.approval import (
            register_gateway_notify,
            set_current_session_key,
        )

        token = set_current_session_key(self._session_key)
        register_gateway_notify(self._session_key, self.approval_notify)
        return token

    def deactivate_approval(self, token) -> None:
        from tools.approval import (
            reset_current_session_key,
            unregister_gateway_notify,
        )

        unregister_gateway_notify(self._session_key)
        try:
            from tools.clarify_gateway import clear_session as clear_clarify_session

            clear_clarify_session(self._session_key)
        except Exception as exc:
            logger.debug("Failed to clear clarify session %s: %s", self._session_key, exc)
        reset_current_session_key(token)

    def send_background_review(self, message: str) -> None:
        if not self._status_adapter or not self._run_still_current():
            return
        if not self._background_release.is_set():
            with self._background_lock:
                if not self._background_release.is_set():
                    self._background_pending.append(message)
                    return
        self._deliver_background_review(message)

    def release_background_reviews(self) -> None:
        self._background_release.set()
        with self._background_lock:
            pending = list(self._background_pending)
            self._background_pending.clear()
        for queued in pending:
            self._deliver_background_review(queued)

    def clarify(self, question: str, choices) -> str:
        from tools import clarify_gateway as clarify_mod

        if not self._status_adapter:
            return ""

        clarify_id = uuid.uuid4().hex[:10]
        clarify_mod.register(
            clarify_id=clarify_id,
            session_key=self._session_key,
            question=question,
            choices=list(choices) if choices else None,
        )

        try:
            self._status_adapter.pause_typing_for_chat(self._status_chat_id)
        except Exception as exc:
            logger.debug("Failed to pause typing before clarify prompt: %s", exc)

        fut = safe_schedule_threadsafe(
            self._status_adapter.send_clarify(
                chat_id=self._status_chat_id,
                question=question,
                choices=list(choices) if choices else None,
                clarify_id=clarify_id,
                session_key=self._session_key,
                metadata=self._status_thread_metadata,
            ),
            self._loop,
            logger=logger,
            log_message="Clarify send failed to schedule",
        )
        send_ok = False
        if fut is not None:
            try:
                result = fut.result(timeout=15)
                send_ok = bool(getattr(result, "success", False))
            except Exception as exc:
                logger.warning("Clarify send failed: %s", exc)

        if not send_ok:
            clarify_mod.clear_session(self._session_key)
            return "[clarify prompt could not be delivered]"

        timeout = clarify_mod.get_clarify_timeout()
        response = clarify_mod.wait_for_response(clarify_id, timeout=float(timeout))
        if response is None or response == "":
            return f"[user did not respond within {int(timeout / 60)}m]"
        return response

    def approval_notify(self, approval_data: dict) -> None:
        if not self._status_adapter:
            return
        self._status_adapter.pause_typing_for_chat(self._status_chat_id)

        command = self._redact_approval_command(approval_data.get("command", ""))
        description = approval_data.get("description", "dangerous command")
        if self._try_button_approval(command, description):
            return
        self._send_text_approval(command, description)

    def _deliver_background_review(self, message: str) -> None:
        if not self._status_adapter or not self._run_still_current():
            return
        safe_schedule_threadsafe(
            self._status_adapter.send(
                self._status_chat_id,
                message,
                metadata=self._status_thread_metadata,
            ),
            self._loop,
            logger=logger,
            log_message="background_review_callback scheduling error",
        )

    def _try_button_approval(self, command: str, description: str) -> bool:
        if getattr(type(self._status_adapter), "send_exec_approval", None) is None:
            return False
        try:
            fut = safe_schedule_threadsafe(
                self._status_adapter.send_exec_approval(
                    chat_id=self._status_chat_id,
                    command=command,
                    session_key=self._session_key,
                    description=description,
                    metadata=self._status_thread_metadata,
                ),
                self._loop,
                logger=logger,
                log_message="send_exec_approval scheduling error",
            )
            if fut is None:
                raise RuntimeError("send_exec_approval: loop unavailable")
            result = fut.result(timeout=15)
            if result.success:
                return True
            logger.warning(
                "Button-based approval failed (send returned error), falling back to text: %s",
                result.error,
            )
        except Exception as exc:
            logger.warning("Button-based approval failed, falling back to text: %s", exc)
        return False

    def _send_text_approval(self, command: str, description: str) -> None:
        command_preview = command[:200] + "..." if len(command) > 200 else command
        message = (
            f"⚠️ **Dangerous command requires approval:**\n"
            f"```\n{command_preview}\n```\n"
            f"Reason: {description}\n\n"
            f"Reply `/approve` to execute, `/approve session` to approve this pattern "
            f"for the session, `/approve always` to approve permanently, or "
            f"`/deny [reason]` to cancel with guidance for the agent."
        )
        try:
            fut = safe_schedule_threadsafe(
                self._status_adapter.send(
                    self._status_chat_id,
                    message,
                    metadata=self._status_thread_metadata,
                ),
                self._loop,
                logger=logger,
                log_message="Approval text-send scheduling error",
            )
            if fut is not None:
                fut.result(timeout=15)
        except Exception as exc:
            logger.error("Failed to send approval request: %s", exc)


def agent_interaction_callbacks_for(**kwargs) -> AgentInteractionCallbacks:
    return AgentInteractionCallbacks(**kwargs)
