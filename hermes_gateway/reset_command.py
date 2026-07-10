"""Gateway /new and /reset command ownership."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

from agent.i18n import t
from channels.platforms.base import MessageEvent
from channels.platforms.base_models import EphemeralReply
from hermes_agent.repositories.session_repo import sanitize_session_title
from hermes_gateway.agent_cache import agent_cache_for
from hermes_gateway.gateway_runtime_config import runtime_config_for
from hermes_gateway.session_runtime_state import session_runtime_state_for

logger = logging.getLogger(__name__)


def _gateway_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _load_gateway_config() -> dict:
    from hermes_agent.gateway.runtime_config import load_gateway_runtime_config

    return load_gateway_runtime_config(_gateway_home())


def _resolve_gateway_model(config: dict | None = None) -> str:
    from hermes_agent.gateway.runtime_config import resolve_gateway_model

    return resolve_gateway_model(config)


def _resolve_runtime_agent_kwargs() -> dict:
    from hermes_agent.gateway.runtime_config import resolve_runtime_agent_kwargs

    return resolve_runtime_agent_kwargs(_gateway_home())


class GatewayResetCommandMixin:
    def _format_session_info(self) -> str:
        """Resolve current model config and return a formatted info block.

        Surfaces model, provider, context length, and endpoint so gateway
        users can immediately see if context detection went wrong (e.g.
        local models falling to the 128K default).
        """
        from agent.model_metadata import get_model_context_length, DEFAULT_FALLBACK_CONTEXT

        model = _resolve_gateway_model()
        config_context_length = None
        provider = None
        base_url = None
        api_key = None
        custom_provs = None
        data = None

        try:
            data = _load_gateway_config()
            if data:
                model_cfg = data.get("model", {})
                if isinstance(model_cfg, dict):
                    raw_ctx = model_cfg.get("context_length")
                    if raw_ctx is not None:
                        try:
                            config_context_length = int(raw_ctx)
                        except (TypeError, ValueError):
                            logger.debug("Suppressed recoverable gateway exception", exc_info=True)
                    provider = model_cfg.get("provider") or None
                    base_url = model_cfg.get("base_url") or None
                try:
                    from hermes_cli.config import get_compatible_custom_providers
                    custom_provs = get_compatible_custom_providers(data)
                except Exception:
                    custom_provs = data.get("custom_providers")
        except Exception:
            logger.debug("Suppressed recoverable gateway exception", exc_info=True)

        # Also check custom_providers for context_length when top-level model.context_length is not set
        if config_context_length is None and data:
            try:
                custom_providers = data.get("custom_providers", [])
                if custom_providers:
                    for cp in custom_providers:
                        if not isinstance(cp, dict):
                            continue
                        cp_model = cp.get("model") or ""
                        cp_models = cp.get("models") or {}
                        # Match provider model to current model
                        if cp_model and cp_model == model:
                            raw_cp_ctx = cp.get("context_length")
                            if raw_cp_ctx is not None:
                                try:
                                    config_context_length = int(raw_cp_ctx)
                                    break
                                except (TypeError, ValueError):
                                    logger.debug("Suppressed recoverable gateway exception", exc_info=True)
                        # Also check per-model context_length
                        if isinstance(cp_models, dict):
                            model_entry = cp_models.get(model)
                            if isinstance(model_entry, dict):
                                model_ctx = model_entry.get("context_length")
                            else:
                                model_ctx = model_entry
                            if model_ctx is not None and isinstance(model_ctx, (int, float)):
                                try:
                                    config_context_length = int(model_ctx)
                                    break
                                except (TypeError, ValueError):
                                    logger.debug("Suppressed recoverable gateway exception", exc_info=True)
            except Exception:
                logger.debug("Suppressed recoverable gateway exception", exc_info=True)

        # Resolve runtime credentials for probing
        try:
            runtime = _resolve_runtime_agent_kwargs()
            provider = provider or runtime.get("provider")
            base_url = base_url or runtime.get("base_url")
            api_key = runtime.get("api_key")
        except Exception:
            logger.debug("Suppressed recoverable gateway exception", exc_info=True)

        context_length = get_model_context_length(
            model,
            base_url=base_url or "",
            api_key=api_key or "",
            config_context_length=config_context_length,
            provider=provider or "",
            custom_providers=custom_provs,
        )

        # Format context source hint
        if config_context_length is not None:
            ctx_source = "config"
        elif context_length == DEFAULT_FALLBACK_CONTEXT:
            ctx_source = "default — set model.context_length in config to override"
        else:
            ctx_source = "detected"

        # Format context length for display
        if context_length >= 1_000_000:
            ctx_display = f"{context_length / 1_000_000:.1f}M"
        elif context_length >= 1_000:
            ctx_display = f"{context_length // 1_000}K"
        else:
            ctx_display = str(context_length)

        lines = [
            f"◆ Model: `{model}`",
            f"◆ Provider: {provider or 'openrouter'}",
            f"◆ Context: {ctx_display} tokens ({ctx_source})",
        ]

        # Show endpoint for local/custom setups
        if base_url and ("localhost" in base_url or "127.0.0.1" in base_url or "0.0.0.0" in base_url):
            lines.append(f"◆ Endpoint: {base_url}")

        return "\n".join(lines)

    async def _handle_reset_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """Handle /new or /reset command."""
        source = event.source
        
        # Get existing session key
        session_key = self._session_key_for_source(source)
        session_runtime_state_for(self).invalidate_session_run_generation(session_key, reason="session_reset")
        session_runtime_state_for(self).release_running_agent_state(session_key)

        # Snapshot the old entry so on_session_finalize can report the
        # expiring session id before reset_session() rotates it.
        old_entry = self.session_store._entries.get(session_key)

        # Close tool resources on the old agent (terminal sandboxes, browser
        # daemons, background processes) before evicting from cache.
        # Guard with getattr because test fixtures may skip __init__.
        _cache_lock = getattr(self, "_agent_cache_lock", None)
        if _cache_lock is not None:
            with _cache_lock:
                _cached = self._agent_cache.get(session_key)
                _old_agent = _cached[0] if isinstance(_cached, tuple) else _cached if _cached else None
            if _old_agent is not None:
                self._cleanup_agent_resources(_old_agent)
        agent_cache_for(self).evict_cached_agent(session_key)

        # Discard any /queue overflow for this session — /new is a
        # conversation-boundary operation, queued follow-ups from the
        # previous conversation must not bleed into the new one.
        _qe = getattr(self, "_queued_events", None)
        if _qe is not None:
            _qe.pop(session_key, None)

        try:
            from tools.env_passthrough import clear_env_passthrough
            clear_env_passthrough()
        except Exception:
            logger.debug("Suppressed recoverable gateway exception", exc_info=True)

        try:
            from tools.credential_files import clear_credential_files
            clear_credential_files()
        except Exception:
            logger.debug("Suppressed recoverable gateway exception", exc_info=True)

        # Reset the session
        new_entry = self.session_store.reset_session(session_key)

        # Clear any session-scoped model/reasoning overrides so the next agent
        # picks up configured defaults instead of previous session switches.
        self._session_model_overrides.pop(session_key, None)
        runtime_config_for(self).set_session_reasoning_override(session_key, None)
        if hasattr(self, "_pending_model_notes"):
            self._pending_model_notes.pop(session_key, None)

        # Clear session-scoped dangerous-command approvals and /yolo state.
        # /new is a conversation-boundary operation — approval state from the
        # previous conversation must not survive the reset.
        session_runtime_state_for(self).clear_session_boundary_security_state(session_key)

        # Fire plugin on_session_finalize hook (session boundary)
        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook
            _old_sid = old_entry.session_id if old_entry else None
            _invoke_hook("on_session_finalize", session_id=_old_sid,
                         platform=source.platform.value if source.platform else "")
        except Exception:
            logger.debug("Suppressed recoverable gateway exception", exc_info=True)

        # Emit session:end hook (session is ending)
        await self.hooks.emit("session:end", {
            "platform": source.platform.value if source.platform else "",
            "user_id": source.user_id,
            "session_key": session_key,
        })

        # Emit session:reset hook
        await self.hooks.emit("session:reset", {
            "platform": source.platform.value if source.platform else "",
            "user_id": source.user_id,
            "session_key": session_key,
        })

        # Resolve session config info to surface to the user
        try:
            session_info = self._format_session_info()
        except Exception:
            session_info = ""

        if new_entry:
            from hermes_gateway.session_navigation_commands import session_navigation_for

            header = session_navigation_for(self).telegram_topic_new_header(source) or t("gateway.reset.header_default")
        else:
            # No existing session, just create one
            new_entry = self.session_store.get_or_create_session(source, force_new=True)
            from hermes_gateway.session_navigation_commands import session_navigation_for

            header = session_navigation_for(self).telegram_topic_new_header(source) or t("gateway.reset.header_new")

        # Set session title if provided with /new <title>
        _title_arg = event.get_command_args().strip()
        _title_note = ""
        if _title_arg and self._session_db and new_entry:
            try:
                sanitized = sanitize_session_title(_title_arg)
            except ValueError as e:
                sanitized = None
                _title_note = t("gateway.reset.title_rejected", error=str(e))
            if sanitized:
                try:
                    self._session_db.sessions.set_title(new_entry.session_id, sanitized)
                    header = t("gateway.reset.header_titled", title=sanitized)
                except ValueError as e:
                    _title_note = t("gateway.reset.title_error_untitled", error=str(e))
                except Exception:
                    logger.debug("Suppressed recoverable gateway exception", exc_info=True)
            elif not _title_note:
                # sanitize_title returned empty (whitespace-only / unprintable)
                _title_note = t("gateway.reset.title_empty_untitled")
        header = header + _title_note

        # When /new runs inside a Telegram DM topic lane, rewrite the
        # (chat_id, thread_id) → session_id binding so the next message
        # uses the freshly-created session. Without this, the binding
        # still points at the old session and the binding-lookup at the
        # top of _handle_message_with_agent would switch right back.
        from hermes_gateway.session_navigation_commands import session_navigation_for

        if session_navigation_for(self).is_telegram_topic_lane(source) and new_entry is not None:
            try:
                session_navigation_for(self).record_telegram_topic_binding(source, new_entry)
            except Exception:
                logger.debug("Failed to rebind Telegram topic after /new", exc_info=True)

        # Fire plugin on_session_reset hook (new session guaranteed to exist)
        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook
            _new_sid = new_entry.session_id if new_entry else None
            _invoke_hook("on_session_reset", session_id=_new_sid,
                         platform=source.platform.value if source.platform else "")
        except Exception:
            logger.debug("Suppressed recoverable gateway exception", exc_info=True)

        # Append a random tip to the reset message
        try:
            from hermes_cli.tips import get_random_tip
            _tip_line = t("gateway.reset.tip", tip=get_random_tip())
        except Exception:
            _tip_line = ""

        if session_info:
            return EphemeralReply(f"{header}\n\n{session_info}{_tip_line}")
        return EphemeralReply(f"{header}{_tip_line}")
