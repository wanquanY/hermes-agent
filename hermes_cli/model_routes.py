"""Authoritative model selection and inference-route resolution."""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from hermes_cli.model_connections import (
    ModelConnectionError,
    ModelConnectionRepository,
    current_local_owner_id,
)
from hermes_cli.model_provider_identity import (
    DOVIE_CLOUD_CONNECTION_ID,
    is_dovie_cloud_provider,
)

SELECTION_SCHEMA_VERSION = 1
ROUTE_SCHEMA_VERSION = 1
ROUTE_KINDS = frozenset({"dovie_cloud", "hermes_direct", "codex_byo"})
_CODEX_PROVIDERS = frozenset({"openai-codex", "codex"})


class ModelRouteError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _text(
    value: Any,
    *,
    field: str,
    maximum: int,
    required: bool = True,
) -> str:
    normalized = str(value or "").strip()
    if required and not normalized:
        raise ModelRouteError(
            "MODEL_SELECTION_INVALID",
            f"{field} is required",
            details={"field": field},
        )
    if len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
        raise ModelRouteError(
            "MODEL_SELECTION_INVALID",
            f"{field} is invalid",
            details={"field": field},
        )
    return normalized


def normalize_model_selection(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ModelRouteError(
            "MODEL_SELECTION_INVALID",
            "model selection must be an object",
        )
    schema_version = int(
        raw.get("schema_version", raw.get("schemaVersion", 0)) or 0
    )
    if schema_version != SELECTION_SCHEMA_VERSION:
        raise ModelRouteError(
            "MODEL_SELECTION_SCHEMA_UNSUPPORTED",
            "unsupported model selection schema",
        )
    expected_route_kind = _text(
        raw.get("expected_route_kind", raw.get("expectedRouteKind")),
        field="expected_route_kind",
        maximum=32,
    )
    if expected_route_kind not in ROUTE_KINDS:
        raise ModelRouteError(
            "MODEL_SELECTION_INVALID",
            "expected_route_kind is invalid",
            details={"field": "expected_route_kind"},
        )
    normalized = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "expected_route_kind": expected_route_kind,
        "connection_id": _text(
            raw.get("connection_id", raw.get("connectionId")),
            field="connection_id",
            maximum=160,
        ),
        "provider_id": _text(
            raw.get("provider_id", raw.get("providerId")),
            field="provider_id",
            maximum=128,
        ),
        "model_id": _text(
            raw.get("model_id", raw.get("modelId")),
            field="model_id",
            maximum=512,
        ),
    }
    catalog_model_id = _text(
        raw.get("catalog_model_id", raw.get("catalogModelId")),
        field="catalog_model_id",
        maximum=512,
        required=False,
    )
    if catalog_model_id:
        normalized["catalog_model_id"] = catalog_model_id
    raw_revision = raw.get(
        "connection_revision",
        raw.get("connectionRevision"),
    )
    if raw_revision is not None:
        try:
            revision = int(raw_revision)
        except (TypeError, ValueError) as exc:
            raise ModelRouteError(
                "MODEL_SELECTION_INVALID",
                "connection_revision must be an integer",
                details={"field": "connection_revision"},
            ) from exc
        if revision < 0:
            raise ModelRouteError(
                "MODEL_SELECTION_INVALID",
                "connection_revision must not be negative",
                details={"field": "connection_revision"},
            )
        normalized["connection_revision"] = revision
    return normalized


def legacy_model_selection(
    *,
    model_id: str,
    provider_id: str,
) -> dict[str, Any]:
    provider = _text(provider_id, field="provider_id", maximum=128)
    route_kind = (
        "dovie_cloud"
        if is_dovie_cloud_provider(provider)
        else "codex_byo"
        if provider.lower() in _CODEX_PROVIDERS
        else "hermes_direct"
    )
    connection_id = (
        DOVIE_CLOUD_CONNECTION_ID
        if route_kind == "dovie_cloud"
        else f"builtin:{provider}"
    )
    return {
        "schema_version": 1,
        "expected_route_kind": route_kind,
        "connection_id": connection_id,
        "provider_id": provider,
        "model_id": _text(model_id, field="model_id", maximum=512),
        "connection_revision": 0,
    }


def default_api_mode(provider_id: str, connection: dict[str, Any] | None) -> str:
    configured = str((connection or {}).get("api_mode") or "").strip()
    if configured:
        return configured
    provider = provider_id.lower()
    if provider in _CODEX_PROVIDERS or provider in {"openai", "xai", "xai-oauth"}:
        return "codex_responses"
    if provider in {"anthropic", "minimax", "minimax-cn", "kimi-coding"}:
        return "anthropic_messages"
    return "chat_completions"


def _fingerprint_payload(
    *,
    owner_id: str,
    selection: dict[str, Any],
    route: dict[str, Any],
    connection_revision: int,
) -> str:
    payload = {
        "schema_version": ROUTE_SCHEMA_VERSION,
        "owner_id": owner_id,
        "selection": selection,
        "route": route,
        "connection_revision": connection_revision,
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class RouteResolutionService:
    """Resolve route identity without reading credential bytes or the network."""

    def __init__(
        self,
        repository: ModelConnectionRepository,
        *,
        owner_id: str | None = None,
    ):
        self.repository = repository
        self.owner_id = owner_id or repository.owner_id

    @classmethod
    def for_runtime(cls) -> "RouteResolutionService":
        repository = ModelConnectionRepository.for_runtime()
        return cls(repository, owner_id=current_local_owner_id())

    def resolve(self, selection: Mapping[str, Any]) -> dict[str, Any]:
        normalized = normalize_model_selection(selection)
        connection_id = normalized["connection_id"]
        provider_id = normalized["provider_id"]
        connection: dict[str, Any] | None = None

        if connection_id == DOVIE_CLOUD_CONNECTION_ID:
            if not is_dovie_cloud_provider(provider_id):
                raise ModelRouteError(
                    "MODEL_CONNECTION_PROVIDER_MISMATCH",
                    "Dovie Cloud connection requires the Dovie Cloud provider",
                )
            route_kind = "dovie_cloud"
            connection_revision = 0
        else:
            try:
                connection = self.repository.get(connection_id)
            except ModelConnectionError as exc:
                if connection_id != f"builtin:{provider_id}":
                    raise ModelRouteError(exc.code, str(exc), details=exc.details) from exc
                connection = None
            if connection is None and connection_id != f"builtin:{provider_id}":
                raise ModelRouteError(
                    "MODEL_CONNECTION_NOT_FOUND",
                    "model connection was not found",
                )
            if connection is not None:
                if not bool(connection.get("enabled", True)):
                    raise ModelRouteError(
                        "MODEL_CONNECTION_DISABLED",
                        "model connection is disabled",
                    )
                actual_provider = str(connection.get("provider_id") or "")
                if actual_provider.lower() != provider_id.lower():
                    raise ModelRouteError(
                        "MODEL_CONNECTION_PROVIDER_MISMATCH",
                        "selection provider does not match the connection",
                    )
                connection_revision = int(
                    connection.get("routing_revision")
                    or connection.get("revision")
                    or 0
                )
                manual_models = connection.get("manual_models")
                if (
                    str(connection.get("kind") or "") == "custom_endpoint"
                    and isinstance(manual_models, dict)
                    and manual_models
                    and normalized["model_id"] not in manual_models
                ):
                    raise ModelRouteError(
                        "MODEL_NOT_AVAILABLE",
                        "model is not registered on the selected custom endpoint",
                    )
            else:
                connection_revision = 0
            route_kind = (
                "codex_byo"
                if provider_id.lower() in _CODEX_PROVIDERS
                else "hermes_direct"
            )

        expected_revision = normalized.get("connection_revision")
        if expected_revision is not None and expected_revision != connection_revision:
            raise ModelRouteError(
                "MODEL_CONNECTION_REVISION_CONFLICT",
                "connection revision changed",
                details={
                    "expected_revision": expected_revision,
                    "actual_revision": connection_revision,
                },
            )
        if normalized["expected_route_kind"] != route_kind:
            raise ModelRouteError(
                "MODEL_ROUTE_ASSERTION_MISMATCH",
                "expected route does not match Hermes route resolution",
                details={
                    "expected_route_kind": normalized["expected_route_kind"],
                    "actual_route_kind": route_kind,
                },
            )
        normalized["connection_revision"] = connection_revision
        route = {
            "route_kind": route_kind,
            "provider_id": provider_id,
            "connection_id": connection_id,
            "model_id": normalized["model_id"],
            "api_mode": default_api_mode(provider_id, connection),
            "billing_authority": (
                "dovie"
                if route_kind == "dovie_cloud"
                else "external_subscription"
                if route_kind == "codex_byo"
                else "upstream_provider"
            ),
            "cloud_usage_policy": (
                "platform_allowed"
                if route_kind == "dovie_cloud"
                else "direct_only"
            ),
        }
        now = datetime.now(timezone.utc)
        return {
            "schema_version": ROUTE_SCHEMA_VERSION,
            "selection": normalized,
            "route": route,
            "connection_revision": connection_revision,
            "route_fingerprint": _fingerprint_payload(
                owner_id=self.owner_id,
                selection=normalized,
                route=route,
                connection_revision=connection_revision,
            ),
            "resolved_at": _iso(now),
        }


@dataclass
class _PreparedResolution:
    payload: dict[str, Any]
    owner_id: str
    conversation_session_id: str
    execution_target: dict[str, Any]
    session_revision: int
    consumed: bool = False


class RunPreparationService:
    """Short-lived, single-consumption route resolution registry."""

    def __init__(
        self,
        route_resolver: RouteResolutionService,
        *,
        ttl_seconds: int = 120,
        maximum_entries: int = 4096,
    ):
        self.route_resolver = route_resolver
        self.ttl_seconds = max(10, min(int(ttl_seconds), 600))
        self.maximum_entries = max(32, int(maximum_entries))
        self._lock = threading.RLock()
        self._entries: dict[str, _PreparedResolution] = {}

    def _prune(self, now: datetime) -> None:
        expired = [
            resolution_id
            for resolution_id, entry in self._entries.items()
            if _parse_iso(entry.payload["expires_at"]) <= now or entry.consumed
        ]
        for resolution_id in expired:
            self._entries.pop(resolution_id, None)
        while len(self._entries) >= self.maximum_entries:
            self._entries.pop(next(iter(self._entries)), None)

    def prepare(
        self,
        *,
        conversation_session_id: str,
        execution_target: Mapping[str, Any] | None,
        session_revision: int,
        selection: Mapping[str, Any],
    ) -> dict[str, Any]:
        conversation = _text(
            conversation_session_id,
            field="conversation_session_id",
            maximum=512,
        )
        target = dict(execution_target or {"kind": "conversation"})
        snapshot = self.route_resolver.resolve(selection)
        now = datetime.now(timezone.utc)
        resolution_id = f"route_{secrets.token_urlsafe(24)}"
        payload = {
            **copy.deepcopy(snapshot),
            "resolution_id": resolution_id,
            "expires_at": _iso(now + timedelta(seconds=self.ttl_seconds)),
            "conversation_session_id": conversation,
            "execution_target": target,
            "session_revision": int(session_revision),
        }
        with self._lock:
            self._prune(now)
            self._entries[resolution_id] = _PreparedResolution(
                payload=payload,
                owner_id=self.route_resolver.owner_id,
                conversation_session_id=conversation,
                execution_target=target,
                session_revision=int(session_revision),
            )
        return copy.deepcopy(payload)

    def consume(
        self,
        resolution_id: str,
        *,
        conversation_session_id: str,
        execution_target: Mapping[str, Any] | None,
        session_revision: int,
        selection: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        normalized_id = _text(
            resolution_id,
            field="resolution_id",
            maximum=256,
        )
        with self._lock:
            entry = self._entries.get(normalized_id)
            if entry is None:
                raise ModelRouteError(
                    "RUN_ROUTE_RESOLUTION_NOT_FOUND",
                    "run route resolution is missing or already consumed",
                )
            if entry.consumed:
                raise ModelRouteError(
                    "RUN_ROUTE_RESOLUTION_CONSUMED",
                    "run route resolution was already consumed",
                )
            if _parse_iso(entry.payload["expires_at"]) <= now:
                self._entries.pop(normalized_id, None)
                raise ModelRouteError(
                    "RUN_ROUTE_RESOLUTION_EXPIRED",
                    "run route resolution expired",
                )
            target = dict(execution_target or {"kind": "conversation"})
            if (
                entry.owner_id != self.route_resolver.owner_id
                or entry.conversation_session_id != conversation_session_id
                or entry.execution_target != target
                or entry.session_revision != int(session_revision)
            ):
                raise ModelRouteError(
                    "RUN_ROUTE_RESOLUTION_BINDING_MISMATCH",
                    "run route resolution does not match this execution",
                )
            current = self.route_resolver.resolve(selection)
            if (
                current["route_fingerprint"]
                != entry.payload["route_fingerprint"]
                or current["connection_revision"]
                != entry.payload["connection_revision"]
            ):
                raise ModelRouteError(
                    "RUN_ROUTE_RESOLUTION_STALE",
                    "model route changed after run preparation",
                )
            entry.consumed = True
            return copy.deepcopy(entry.payload)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


__all__ = [
    "ModelRouteError",
    "ROUTE_KINDS",
    "RouteResolutionService",
    "RunPreparationService",
    "default_api_mode",
    "legacy_model_selection",
    "normalize_model_selection",
]
