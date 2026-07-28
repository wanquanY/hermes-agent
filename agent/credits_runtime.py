"""AIAgent bridge for credits headers and out-of-band driver notices."""

from __future__ import annotations

import logging
import os
from typing import Any

from utils import is_truthy_value


logger = logging.getLogger(__name__)


class CreditsRuntimeMixin:
    """Keep credit policy out of the agent orchestrator and fail open on UI I/O."""

    def _emit_notice(self, notice: Any) -> None:
        callback = getattr(self, "notice_callback", None)
        if callable(callback):
            try:
                callback(notice)
            except Exception:
                logger.debug("notice callback failed", exc_info=True)

    def _emit_notice_clear(self, key: str) -> None:
        callback = getattr(self, "notice_clear_callback", None)
        if callable(callback):
            try:
                callback(key)
            except Exception:
                logger.debug("notice-clear callback failed", exc_info=True)

    def _credits_notices_enabled(self) -> bool:
        cached = getattr(self, "_credits_notices_enabled_cache", None)
        if cached is not None:
            return bool(cached)
        enabled = True
        try:
            from hermes_cli.config import load_config

            config = load_config() or {}
            display = config.get("display") if isinstance(config, dict) else None
            if isinstance(display, dict) and "credits_notices" in display:
                enabled = bool(display.get("credits_notices"))
        except Exception:
            enabled = True
        self._credits_notices_enabled_cache = enabled
        return enabled

    def _emit_credits_notices(self) -> None:
        if not any(
            callable(getattr(self, name, None))
            for name in ("notice_callback", "notice_clear_callback")
        ):
            return
        if not self._credits_notices_enabled():
            return
        state = getattr(self, "_credits_state", None)
        if state is None:
            return
        try:
            from agent.credits_tracker import (
                evaluate_credits_notices,
                is_free_tier_model,
            )

            latch = getattr(self, "_credits_latch", None)
            if not isinstance(latch, dict):
                latch = self._credits_latch = {
                    "active": set(),
                    "seen_below_90": False,
                    "usage_band": None,
                }
            shows, clears = evaluate_credits_notices(
                state,
                latch,
                model_is_free=is_free_tier_model(
                    str(getattr(self, "model", "") or ""),
                    str(getattr(self, "base_url", "") or ""),
                ),
            )
            for key in clears:
                self._emit_notice_clear(key)
            for notice in shows:
                self._emit_notice(notice)
        except Exception:
            logger.warning("credits notice evaluation failed", exc_info=True)

    def _capture_credits(self, http_response: Any) -> None:
        """Retain the last valid credit state and reconcile notices."""
        try:
            from agent.credits_tracker import (
                dev_fixture_credits_state,
                parse_credits_headers,
            )

            state = dev_fixture_credits_state()
            if state is None:
                headers = getattr(http_response, "headers", None)
                if not headers:
                    return
                state = parse_credits_headers(
                    headers,
                    provider=str(getattr(self, "provider", "") or ""),
                )
            if state is None:
                return
            self._credits_state = state
            if getattr(self, "_credits_session_start_micros", None) is None:
                self._credits_session_start_micros = state.remaining_micros
            if is_truthy_value(os.getenv("HERMES_DEV_CREDITS")):
                logger.info(
                    "credits capture remaining=%d paid=%s used=%s",
                    state.remaining_micros,
                    state.paid_access,
                    state.used_fraction,
                )
            self._emit_credits_notices()
        except Exception:
            logger.debug("credits capture failed open", exc_info=True)

    def get_credits_state(self) -> Any:
        return getattr(self, "_credits_state", None)

    def get_credits_spent_micros(self) -> int | None:
        start = getattr(self, "_credits_session_start_micros", None)
        state = getattr(self, "_credits_state", None)
        if start is None or state is None:
            return None
        return int(start) - int(state.remaining_micros)


__all__ = ["CreditsRuntimeMixin"]
