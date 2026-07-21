"""Profile-aware model routing for the OpenAI-compatible API surface."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

from channels.config import Platform
from channels.session_identity import SessionSource
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class APIModelRoute:
    """One immutable client-model alias and its upstream runtime overrides."""

    alias: str
    model: str
    provider: Optional[str] = None
    api_key: Optional[str] = field(default=None, repr=False)
    base_url: Optional[str] = None


def parse_api_model_routes(raw: Any) -> Mapping[str, APIModelRoute]:
    """Validate a ``model_routes`` mapping without exposing route secrets."""
    if not isinstance(raw, dict):
        if raw:
            logger.warning(
                "api_server model_routes ignored: expected a mapping, got %s",
                type(raw).__name__,
            )
        return MappingProxyType({})

    routes: dict[str, APIModelRoute] = {}
    for alias, value in raw.items():
        alias_text = str(alias).strip()
        if not alias_text or not isinstance(value, dict):
            logger.warning(
                "api_server model_routes: dropping invalid route entry %r",
                alias_text or alias,
            )
            continue
        fields = {
            name: str(value[name]).strip()
            for name in ("model", "provider", "api_key", "base_url")
            if value.get(name) is not None and str(value[name]).strip()
        }
        model = fields.get("model")
        if not model:
            logger.warning(
                "api_server model_routes: route %r has no model; dropping",
                alias_text,
            )
            continue
        routes[alias_text] = APIModelRoute(
            alias=alias_text,
            model=model,
            provider=fields.get("provider"),
            api_key=fields.get("api_key"),
            base_url=fields.get("base_url"),
        )
    return MappingProxyType(routes)


class APIServerModelRoutesMixin:
    """Resolve API model aliases inside the current profile runtime scope."""

    def _initialize_model_routes(self, raw: Any) -> None:
        home = str(Path(get_hermes_home()).resolve())
        self._model_routes_by_home: dict[str, Mapping[str, APIModelRoute]] = {
            home: parse_api_model_routes(raw),
        }

    def _current_model_routes(self) -> Mapping[str, APIModelRoute]:
        home = str(Path(get_hermes_home()).resolve())
        cached = self._model_routes_by_home.get(home)
        if cached is not None:
            return cached

        from hermes_gateway.config import load_gateway_config

        config = load_gateway_config()
        platform_config = config.platforms.get(Platform.API_SERVER)
        raw = (platform_config.extra or {}).get("model_routes") if platform_config else None
        routes = parse_api_model_routes(raw)
        self._model_routes_by_home[home] = routes
        return routes

    def _resolve_route(self, model_alias: Any) -> Optional[APIModelRoute]:
        if not isinstance(model_alias, str):
            return None
        return self._current_model_routes().get(model_alias)

    def _advertised_model_name(self) -> str:
        """Return the primary model id for the current prefixed profile."""
        profile = self._request_profile()
        if not profile:
            return self._model_name

        from agent.secret_scope import get_profile_env
        from hermes_gateway.config import load_gateway_config

        config = load_gateway_config()
        platform_config = config.platforms.get(Platform.API_SERVER)
        extra = platform_config.extra if platform_config else {}
        explicit = str(
            (extra or {}).get("model_name")
            or get_profile_env("API_SERVER_MODEL_NAME", "")
            or ""
        ).strip()
        return explicit or profile

    def _session_model_override_for(
        self,
        session_key: Optional[str],
    ) -> Optional[dict[str, Any]]:
        """Read a session override without crossing a profile namespace."""
        if not session_key:
            return None
        runner = getattr(self, "gateway_runner", None)
        overrides = getattr(runner, "_session_model_overrides", None)
        if not isinstance(overrides, dict):
            return None

        key = str(session_key)
        if not key.startswith("agent:") and runner is not None:
            source = SessionSource(
                platform=Platform.API_SERVER,
                chat_id=key,
                profile=self._request_profile(),
            )
            try:
                key = runner._session_key_for_source(source)
            except Exception:
                key = str(session_key)

        override = overrides.get(key)
        if override is None and not getattr(
            getattr(runner, "config", None),
            "multiplex_profiles",
            False,
        ):
            # Compatibility for historical single-profile callers that stored
            # the external API session key directly.
            override = overrides.get(str(session_key))
        return dict(override) if isinstance(override, dict) else None

    @staticmethod
    def _apply_session_override(
        model: str,
        runtime_kwargs: dict[str, Any],
        override: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        effective = dict(runtime_kwargs)
        model = str(override.get("model") or model)
        for key in ("provider", "api_key", "base_url", "api_mode"):
            if override.get(key) is not None:
                effective[key] = override[key]
        return model, effective

    @staticmethod
    def _apply_model_route(
        model: str,
        runtime_kwargs: dict[str, Any],
        route: APIModelRoute,
    ) -> tuple[str, dict[str, Any]]:
        """Apply a route using the canonical provider credential resolver."""
        effective = dict(runtime_kwargs)
        if route.provider:
            from hermes_agent.gateway.runtime_config import (
                resolve_runtime_agent_kwargs_for_provider,
            )

            effective.update(
                resolve_runtime_agent_kwargs_for_provider(
                    route.provider,
                    explicit_api_key=route.api_key,
                    explicit_base_url=route.base_url,
                )
            )
        if route.api_key:
            effective["api_key"] = route.api_key
        if route.base_url:
            effective["base_url"] = route.base_url
        return route.model, effective
