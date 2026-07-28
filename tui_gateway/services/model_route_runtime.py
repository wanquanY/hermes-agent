"""Gateway integration for Hermes-owned model selections and run routes."""

from __future__ import annotations

import copy
import json
import threading
from typing import Any, Mapping

from hermes_cli.model_routes import (
    ModelRouteError,
    RouteResolutionService,
    RunPreparationService,
    legacy_model_selection,
    normalize_model_selection,
)

_SERVICE_LOCK = threading.Lock()
_SERVICE_KEY = ""
_RUN_PREPARATION_SERVICE: RunPreparationService | None = None


def _model_config(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return {}
    raw = row.get("model_config")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _service() -> RunPreparationService:
    from hermes_cli.model_connections import (
        control_plane_home,
        current_local_owner_id,
    )

    global _RUN_PREPARATION_SERVICE, _SERVICE_KEY
    owner_id = current_local_owner_id()
    key = f"{control_plane_home()}::{owner_id}"
    with _SERVICE_LOCK:
        if _RUN_PREPARATION_SERVICE is None or _SERVICE_KEY != key:
            _RUN_PREPARATION_SERVICE = RunPreparationService(
                RouteResolutionService.for_runtime()
            )
            _SERVICE_KEY = key
        return _RUN_PREPARATION_SERVICE


def route_snapshot(selection: Mapping[str, Any]) -> dict[str, Any]:
    return _service().route_resolver.resolve(selection)


def normalize_create_selection(
    raw_selection: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Normalize an optional session-create selection and resolve its route."""

    if raw_selection is None:
        return None, None
    if not isinstance(raw_selection, Mapping):
        raise ModelRouteError(
            "MODEL_SELECTION_INVALID",
            "model_selection must be an object",
        )
    snapshot = route_snapshot(normalize_model_selection(raw_selection))
    return dict(snapshot["selection"]), snapshot


def route_config_fields(
    selection: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    *,
    revision: int = 1,
) -> dict[str, Any]:
    """Build the durable route portion of a session runtime config."""

    if not selection or not snapshot:
        return {}
    normalized = normalize_model_selection(selection)
    return {
        "model": normalized["model_id"],
        "provider": normalized["provider_id"],
        "connection_id": normalized["connection_id"],
        "model_selection": normalized,
        "model_selection_revision": int(revision),
        "route_snapshot": copy.deepcopy(dict(snapshot)),
    }


def route_result_fields(
    snapshot: Mapping[str, Any] | None,
    *,
    revision: int = 1,
) -> dict[str, Any]:
    """Build the public, secret-free route fields for create/status results."""

    if not snapshot:
        return {}
    return {**copy.deepcopy(dict(snapshot)), "session_revision": int(revision)}


def _selection_scope_key(
    execution_target: Mapping[str, Any] | None,
) -> str:
    target = execution_target if isinstance(execution_target, Mapping) else {}
    kind = str(target.get("kind") or "conversation").strip()
    if kind != "team_member":
        return ""
    member_id = str(
        target.get("target_member_id")
        or target.get("targetMemberId")
        or ""
    ).strip()
    if not member_id:
        raise ModelRouteError(
            "MODEL_EXECUTION_TARGET_INVALID",
            "team member execution target requires target_member_id",
        )
    return f"team_member:{member_id}"


def _scoped_selection(
    source: Mapping[str, Any],
    scope_key: str,
) -> tuple[dict[str, Any], int] | None:
    if not scope_key:
        return None
    raw_scopes = source.get("model_selections")
    if not isinstance(raw_scopes, Mapping):
        return None
    raw = raw_scopes.get(scope_key)
    if not isinstance(raw, Mapping):
        return None
    selection = raw.get("selection")
    if not isinstance(selection, Mapping):
        return None
    return (
        normalize_model_selection(selection),
        int(raw.get("revision") or 1),
    )


def scoped_selections_from_state(
    *,
    conversation_session_id: str,
    live_session: Mapping[str, Any] | None,
    db: Any,
) -> dict[str, dict[str, Any]]:
    """Project persisted target selections without leaking route credentials.

    Stored state may contain route snapshots and future operational metadata.
    ``session.status`` only needs the normalized selection identity and its
    optimistic-concurrency revision, so keep that public projection narrow.
    Live values win over the durable row while an attached gateway session is
    active.
    """

    merged: dict[str, Any] = {}
    row = None
    if db is not None and conversation_session_id:
        row = db.sessions.get(conversation_session_id)
    config = _model_config(row)
    persisted = config.get("model_selections")
    if isinstance(persisted, Mapping):
        merged.update(persisted)
    if isinstance(live_session, Mapping):
        live = live_session.get("model_selections")
        if isinstance(live, Mapping):
            merged.update(live)

    projected: dict[str, dict[str, Any]] = {}
    for raw_scope, raw_entry in merged.items():
        scope = str(raw_scope or "").strip()
        if not scope.startswith("team_member:") or not isinstance(raw_entry, Mapping):
            continue
        selection = raw_entry.get("selection")
        if not isinstance(selection, Mapping):
            continue
        try:
            projected[scope] = {
                "selection": normalize_model_selection(selection),
                "revision": max(int(raw_entry.get("revision") or 1), 1),
            }
        except (ModelRouteError, TypeError, ValueError):
            continue
    return projected


def selection_from_state(
    *,
    conversation_session_id: str,
    live_session: Mapping[str, Any] | None,
    db: Any,
    execution_target: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    scope_key = _selection_scope_key(execution_target)
    if isinstance(live_session, Mapping):
        scoped = _scoped_selection(live_session, scope_key)
        if scoped is not None:
            return scoped
        raw_selection = live_session.get("model_selection")
        if not scope_key and isinstance(raw_selection, Mapping):
            return (
                normalize_model_selection(raw_selection),
                int(live_session.get("model_selection_revision") or 1),
            )
    row = None
    if db is not None and conversation_session_id:
        row = db.sessions.get(conversation_session_id)
    config = _model_config(row)
    scoped = _scoped_selection(config, scope_key)
    if scoped is not None:
        return scoped
    raw_selection = config.get("model_selection")
    if not scope_key and isinstance(raw_selection, Mapping):
        return (
            normalize_model_selection(raw_selection),
            int(config.get("model_selection_revision") or 1),
        )
    if scope_key:
        raise ModelRouteError(
            "MODEL_SELECTION_NOT_CONFIGURED",
            f"execution target {scope_key} has no persisted model selection",
        )
    live_model = (
        live_session.get("model")
        if isinstance(live_session, Mapping)
        else None
    )
    live_provider = (
        live_session.get("provider")
        if isinstance(live_session, Mapping)
        else None
    )
    model = str(
        live_model
        or (row or {}).get("model")
        or config.get("model")
        or ""
    ).strip()
    provider = str(live_provider or config.get("provider") or "").strip()
    if not model or not provider:
        # Pre-selection-schema sessions persisted only the model id in their
        # session row. Their profile-scoped Hermes config is still the
        # authoritative legacy source for the matching provider.
        try:
            from hermes_cli.config import load_config

            configured_model = load_config().get("model") or {}
        except Exception:
            configured_model = {}
        if isinstance(configured_model, Mapping):
            model = model or str(
                configured_model.get("default")
                or configured_model.get("name")
                or ""
            ).strip()
            provider = provider or str(configured_model.get("provider") or "").strip()
    if not model or not provider:
        raise ModelRouteError(
            "MODEL_SELECTION_NOT_CONFIGURED",
            "session has no persisted model selection",
        )
    return legacy_model_selection(model_id=model, provider_id=provider), 0


def route_status_fields(
    *,
    conversation_session_id: str,
    live_session: Mapping[str, Any] | None,
    db: Any,
) -> dict[str, Any]:
    """Return a safe route status projection for both modern and legacy rows."""

    try:
        scoped_selections = scoped_selections_from_state(
            conversation_session_id=conversation_session_id,
            live_session=live_session,
            db=db,
        )
        selection, revision = selection_from_state(
            conversation_session_id=conversation_session_id,
            live_session=live_session,
            db=db,
        )
    except Exception:
        # Legacy sessions may not yet contain enough provider metadata. Keep
        # their status readable so Desktop can drive an explicit migration.
        return {}
    return {
        **route_snapshot(selection),
        "session_revision": revision,
        "model_selections": scoped_selections,
    }


def persist_selection(
    *,
    db: Any,
    conversation_session_id: str,
    selection: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    revision: int,
    existing_config: Mapping[str, Any] | None = None,
    execution_target: Mapping[str, Any] | None = None,
) -> None:
    if db is None:
        return
    config = dict(existing_config or {})
    if not config:
        config = _model_config(db.sessions.get(conversation_session_id))
    normalized = normalize_model_selection(selection)
    scope_key = _selection_scope_key(execution_target)
    if scope_key:
        scoped = dict(config.get("model_selections") or {})
        scoped[scope_key] = {
            "selection": normalized,
            "revision": int(revision),
            "route_snapshot": copy.deepcopy(dict(snapshot)),
        }
        config["model_selections"] = scoped
        db.sessions.update_runtime_config(
            conversation_session_id,
            config,
        )
        return
    config.update(
        {
            "model": normalized["model_id"],
            "provider": normalized["provider_id"],
            "connection_id": normalized["connection_id"],
            "model_selection": normalized,
            "model_selection_revision": int(revision),
            "route_snapshot": copy.deepcopy(dict(snapshot)),
        }
    )
    db.sessions.update_runtime_config(
        conversation_session_id,
        config,
        model=normalized["model_id"],
    )


def prepare_run_route(
    *,
    conversation_session_id: str,
    execution_target: Mapping[str, Any] | None,
    expected_session_revision: int | None,
    live_session: Mapping[str, Any] | None,
    db: Any,
) -> dict[str, Any]:
    selection, revision = selection_from_state(
        conversation_session_id=conversation_session_id,
        live_session=live_session,
        db=db,
        execution_target=execution_target,
    )
    if (
        expected_session_revision is not None
        and int(expected_session_revision) != revision
    ):
        raise ModelRouteError(
            "MODEL_SESSION_REVISION_CONFLICT",
            "session model selection revision changed",
            details={
                "expected_revision": int(expected_session_revision),
                "actual_revision": revision,
            },
        )
    return _service().prepare(
        conversation_session_id=conversation_session_id,
        execution_target=execution_target,
        session_revision=revision,
        selection=selection,
    )


def consume_run_route(
    *,
    resolution_id: str,
    conversation_session_id: str,
    execution_target: Mapping[str, Any] | None,
    live_session: Mapping[str, Any] | None,
    db: Any,
) -> dict[str, Any]:
    selection, revision = selection_from_state(
        conversation_session_id=conversation_session_id,
        live_session=live_session,
        db=db,
        execution_target=execution_target,
    )
    return _service().consume(
        resolution_id,
        conversation_session_id=conversation_session_id,
        execution_target=execution_target,
        session_revision=revision,
        selection=selection,
    )


def route_error_response(rid: Any, error: Exception, *, fallback_code: int = 4412):
    if isinstance(error, ModelRouteError):
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {
                "code": fallback_code,
                "message": str(error),
                "data": {
                    "error_code": error.code,
                    "details": error.details,
                },
            },
        }
    return None


__all__ = [
    "consume_run_route",
    "normalize_create_selection",
    "persist_selection",
    "prepare_run_route",
    "route_config_fields",
    "route_error_response",
    "route_result_fields",
    "route_snapshot",
    "route_status_fields",
    "selection_from_state",
]
