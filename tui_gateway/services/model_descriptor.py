from __future__ import annotations

from typing import Any


def _apply_descriptor_context_window(agent: Any, context_window: Any) -> None:
    """Calibrate the context compressor to the model's admin-configured window.

    The desktop model catalog ships each model's real context window (from the
    admin model config) in the descriptor on every prompt.submit. Without this,
    the compressor falls back to model_metadata's static family-pattern guess
    (e.g. 1,048,576 for an unrecognised renamed model), so the auto-compaction
    threshold (context_length × threshold_percent) is calibrated to the wrong
    window — it compresses far too late and only the reactive context-limit-error
    path saves the turn. Treating the descriptor as the authoritative
    `config_context_length` makes proactive compression track the configured
    window automatically — no manual `model.context_length` in config.yaml.

    Skipped when a reactive probe has already discovered a real (smaller) limit
    from a provider overflow error, so we never re-inflate past a proven ceiling.
    """
    if not isinstance(context_window, int) or context_window <= 0 or agent is None:
        return
    # Authoritative override consumed by every get_model_context_length() call.
    setattr(agent, "_config_context_length", context_window)
    comp = getattr(agent, "context_compressor", None)
    if comp is None:
        return
    if getattr(comp, "context_length", None) == context_window:
        return
    if getattr(comp, "_context_probed", False) and context_window > getattr(comp, "context_length", 0):
        return
    try:
        comp.update_model(
            model=getattr(agent, "model", "") or "",
            context_length=context_window,
            base_url=getattr(agent, "base_url", "") or "",
            api_key=getattr(agent, "api_key", "") or "",
            provider=getattr(agent, "provider", "") or "",
            api_mode=getattr(agent, "api_mode", "") or "",
        )
    except Exception:
        pass


def normalize_model_descriptor(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    descriptor_id = str(raw.get("id") or "").strip()
    if not descriptor_id:
        return {}

    descriptor: dict[str, Any] = {"id": descriptor_id}
    for key in (
        "name",
        "provider",
        "api_provider",
        "api_format",
        "catalog_source",
        "reasoning_format",
    ):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            descriptor[key] = value.strip()

    context_window = raw.get("context_window")
    if isinstance(context_window, (int, float)) and context_window > 0:
        descriptor["context_window"] = int(context_window)

    for key in (
        "vision_enabled",
        "reasoning_enabled",
        "reasoning_extra_body_enabled",
        "responses_api_required_for_reasoning_with_tools",
    ):
        value = raw.get(key)
        if isinstance(value, bool):
            descriptor[key] = value

    request_params = raw.get("request_params")
    if isinstance(request_params, dict):
        descriptor["request_params"] = dict(request_params)

    return descriptor


def authoritative_catalog_model_id(raw: object) -> str:
    """Return the model id when a caller supplied catalog authority.

    Gateway clients such as Dovie resolve their selectable models from an
    authenticated control-plane registry before calling ``model.set``.  The
    descriptor carries that provenance explicitly so the local runtime can
    consume the registry decision instead of synchronously probing the remote
    inference endpoint's optional ``/models`` surface again.

    A bare ``{"id": ...}`` descriptor remains metadata-only and therefore
    does not bypass the interactive custom-endpoint validation path.
    """
    descriptor = normalize_model_descriptor(raw)
    catalog_source = str(descriptor.get("catalog_source") or "").strip()
    return str(descriptor.get("id") or "").strip() if catalog_source else ""


def set_session_model_descriptor(
    session: dict[str, Any],
    descriptor: dict[str, Any],
    *,
    clear_if_empty: bool = False,
) -> None:
    normalized = normalize_model_descriptor(descriptor)
    agent = session.get("agent") if session else None
    if normalized:
        session["model_descriptor"] = normalized
        if agent is not None:
            setattr(agent, "model_descriptor", normalized)
            _apply_descriptor_context_window(agent, normalized.get("context_window"))
        return
    if clear_if_empty:
        session.pop("model_descriptor", None)
        if agent is not None and hasattr(agent, "model_descriptor"):
            setattr(agent, "model_descriptor", {})
