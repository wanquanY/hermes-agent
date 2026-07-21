"""Optional registration surfaces exposed through ``PluginContext``.

The core plugin loader owns discovery and lifecycle.  This mixin keeps
provider-specific extension protocols out of that loader so adding a new
backend category does not grow ``hermes_cli.plugins`` into another monolith.
Imports are lazy by design: a process that never uses voice, dashboard auth,
secret sources, Slack actions, or middleware pays no import cost for them.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class PluginContextRegistrationsMixin:
    """Provider and behavior registrations mixed into ``PluginContext``."""

    manifest: Any
    _manager: Any

    def register_dashboard_auth_provider(self, provider: Any) -> None:
        """Register a typed dashboard authentication provider."""

        from hermes_cli.dashboard_auth import DashboardAuthProvider, register_provider

        if not isinstance(provider, DashboardAuthProvider):
            logger.warning(
                "Plugin '%s' tried to register an invalid DashboardAuthProvider",
                self.manifest.name,
            )
            return
        try:
            register_provider(provider)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Plugin '%s' failed to register dashboard-auth provider %r: %s",
                self.manifest.name,
                getattr(provider, "name", "?"),
                exc,
            )
            return
        logger.info(
            "Plugin '%s' registered dashboard-auth provider: %s",
            self.manifest.name,
            provider.name,
        )

    def register_secret_source(self, source: Any) -> None:
        """Register a validated external secret-manager source."""

        from agent.secret_sources.base import SecretSource
        from agent.secret_sources.registry import register_source

        if not isinstance(source, SecretSource):
            logger.warning(
                "Plugin '%s' tried to register an invalid secret source",
                self.manifest.name,
            )
            return
        if register_source(source):
            logger.info(
                "Plugin '%s' registered secret source: %s",
                self.manifest.name,
                source.name,
            )

    def register_tts_provider(self, provider: Any) -> None:
        """Register a plugin text-to-speech backend."""

        from agent.tts_provider import TTSProvider
        from agent.tts_registry import register_provider

        if not isinstance(provider, TTSProvider):
            logger.warning(
                "Plugin '%s' tried to register an invalid TTS provider",
                self.manifest.name,
            )
            return
        register_provider(provider)
        logger.info(
            "Plugin '%s' registered TTS provider: %s",
            self.manifest.name,
            provider.name,
        )

    def register_transcription_provider(self, provider: Any) -> None:
        """Register a plugin speech-to-text backend."""

        from agent.transcription_provider import TranscriptionProvider
        from agent.transcription_registry import register_provider

        if not isinstance(provider, TranscriptionProvider):
            logger.warning(
                "Plugin '%s' tried to register an invalid transcription provider",
                self.manifest.name,
            )
            return
        register_provider(provider)
        logger.info(
            "Plugin '%s' registered transcription provider: %s",
            self.manifest.name,
            provider.name,
        )

    def register_slack_action_handler(
        self,
        action_id: Any,
        callback: Callable[..., Any],
    ) -> None:
        """Register a Slack Block Kit action callback for adapter startup."""

        if not callable(callback):
            raise ValueError(
                f"Plugin '{self.manifest.name}' registered a non-callable Slack action"
            )
        if action_id is None or (
            isinstance(action_id, str) and not action_id.strip()
        ):
            raise ValueError(
                f"Plugin '{self.manifest.name}' registered an empty Slack action_id"
            )
        self._manager._slack_action_handlers.append(
            (action_id, callback, self.manifest.name)
        )

    def register_auxiliary_task(
        self,
        key: str,
        *,
        display_name: str,
        description: str,
        defaults: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register a plugin-owned auxiliary LLM routing task."""

        if not isinstance(key, str) or not key:
            raise ValueError(
                f"Plugin '{self.manifest.name}' registered invalid auxiliary key {key!r}"
            )
        if not all(character.isalnum() or character == "_" for character in key):
            raise ValueError(
                f"Plugin '{self.manifest.name}' auxiliary key {key!r} must use "
                "only alphanumeric characters and underscores"
            )

        from hermes_cli.auxiliary_tasks import RESERVED_AUXILIARY_TASK_KEYS

        if key in RESERVED_AUXILIARY_TASK_KEYS:
            raise ValueError(
                f"Plugin '{self.manifest.name}' cannot register auxiliary task "
                f"{key!r} — that key is reserved for a built-in task"
            )
        existing = self._manager._aux_tasks.get(key)
        if existing is not None and existing.get("plugin") != self.manifest.name:
            raise ValueError(
                f"Plugin '{self.manifest.name}' cannot register auxiliary task "
                f"{key!r} — already registered by plugin '{existing.get('plugin')}'"
            )

        merged_defaults: Dict[str, Any] = {
            "provider": "auto",
            "model": "",
            "base_url": "",
            "api_key": "",
            "timeout": 60,
            "extra_body": {},
        }
        if defaults:
            merged_defaults.update(defaults)
        self._manager._aux_tasks[key] = {
            "key": key,
            "display_name": display_name,
            "description": description,
            "defaults": merged_defaults,
            "plugin": self.manifest.name,
        }

    def register_middleware(self, kind: str, callback: Callable[..., Any]) -> None:
        """Register behavior-changing middleware separately from observers."""

        from hermes_cli.middleware import VALID_MIDDLEWARE

        if not callable(callback):
            raise ValueError(
                f"Plugin '{self.manifest.name}' registered non-callable middleware"
            )
        if kind not in VALID_MIDDLEWARE:
            logger.warning(
                "Plugin '%s' registered unknown middleware '%s' (valid: %s)",
                self.manifest.name,
                kind,
                ", ".join(sorted(VALID_MIDDLEWARE)),
            )
        self._manager._middleware.setdefault(kind, []).append(callback)
