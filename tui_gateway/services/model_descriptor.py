from __future__ import annotations

from typing import Any

from hermes_constants import parse_reasoning_effort


_RESERVED_REQUEST_PARAM_KEYS = {
    "messages",
    "model",
    "stream",
    "stream_options",
    "timeout",
    "tools",
}

_MODEL_API_FORMAT_TO_HERMES_API_MODE = {
    "openai": "chat_completions",
    "openai_responses": "codex_responses",
    "codex_responses": "codex_responses",
}
_EXECUTOR_OWNED_API_MODES = frozenset({"codex_app_server"})


def model_descriptor_api_mode(raw: object) -> str:
    """Resolve a catalog wire format to Hermes' transport name.

    The catalog owns protocol selection for ambient/cloud runtimes. Managed
    connections own their endpoint and wire protocol as one atomic route, so
    their descriptors are capability metadata only. Unknown/native formats
    intentionally return an empty value so the provider runtime keeps
    authority until a dedicated Hermes transport exists for that protocol.
    """
    if not isinstance(raw, dict):
        return ""
    api_format = str(raw.get("api_format") or "").strip().lower()
    return _MODEL_API_FORMAT_TO_HERMES_API_MODE.get(api_format, "")


def _clear_descriptor_api_mode_tracking(agent: Any) -> None:
    setattr(agent, "_model_descriptor_api_mode_base", "")
    setattr(agent, "_model_descriptor_api_mode_applied", "")


def _restore_descriptor_api_mode(agent: Any) -> None:
    """Restore the provider runtime mode shadowed by the last descriptor."""
    applied = str(
        getattr(agent, "_model_descriptor_api_mode_applied", "") or ""
    ).strip()
    base = str(
        getattr(agent, "_model_descriptor_api_mode_base", "") or ""
    ).strip()
    current = str(getattr(agent, "api_mode", "") or "").strip()
    _clear_descriptor_api_mode_tracking(agent)
    if not applied or not base or current != applied or current == base:
        return
    from agent.agent_runtime_helpers import switch_openai_wire_api_mode

    switch_openai_wire_api_mode(agent, base)


def _apply_descriptor_api_mode(agent: Any, descriptor: dict[str, Any]) -> None:
    if agent is None:
        return
    raw_connection_id = getattr(agent, "_managed_connection_id", "")
    if isinstance(raw_connection_id, str) and raw_connection_id.strip():
        # A managed connection resolves credential, endpoint, and protocol as
        # one route. Restore any descriptor layer that predated the managed
        # binding, then leave the provider runtime authoritative.
        _restore_descriptor_api_mode(agent)
        return
    target = model_descriptor_api_mode(descriptor)
    current = str(getattr(agent, "api_mode", "") or "").strip().lower()
    if current in _EXECUTOR_OWNED_API_MODES:
        return

    applied = str(
        getattr(agent, "_model_descriptor_api_mode_applied", "") or ""
    ).strip()
    if target and current == target and applied == target:
        return

    _restore_descriptor_api_mode(agent)
    if not target:
        return

    current = str(getattr(agent, "api_mode", "") or "").strip().lower()
    if current == target:
        return
    from agent.agent_runtime_helpers import switch_openai_wire_api_mode

    switch_openai_wire_api_mode(agent, target)
    setattr(agent, "_model_descriptor_api_mode_base", current)
    setattr(agent, "_model_descriptor_api_mode_applied", target)


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


def _apply_descriptor_reasoning_config(agent: Any, descriptor: dict[str, Any]) -> None:
    """Apply the registry-validated desktop reasoning choice to the agent."""
    if agent is None:
        return
    if descriptor.get("reasoning_enabled") is False:
        setattr(agent, "reasoning_config", {"enabled": False})
        return
    effort = str(descriptor.get("reasoning_effort") or "").strip().lower()
    if not effort:
        if descriptor.get("reasoning_enabled") is True:
            # A reasoning-capable model without an explicit selection uses its
            # provider/model default. Clear any override left by the previous
            # model instead of leaking that model's effort across a switch.
            setattr(agent, "reasoning_config", None)
        return
    supported = descriptor.get("reasoning_efforts")
    if isinstance(supported, list) and supported and effort not in supported:
        setattr(agent, "reasoning_config", None)
        return
    parsed = parse_reasoning_effort(effort)
    if parsed is not None:
        setattr(agent, "reasoning_config", parsed)


def _restore_descriptor_request_overrides(agent: Any) -> dict[str, Any]:
    """Remove the previous descriptor layer without disturbing agent settings.

    ``request_overrides`` also carries independent session settings such as
    service tier.  A model switch must therefore restore values shadowed by the
    previous descriptor instead of replacing the whole mapping or blindly
    deleting keys.
    """
    overrides = dict(getattr(agent, "request_overrides", None) or {})
    shadowed = getattr(agent, "_model_descriptor_request_override_shadow", None)
    if not isinstance(shadowed, dict):
        return overrides
    for key, state in shadowed.items():
        if not isinstance(state, tuple) or len(state) != 2:
            continue
        existed, value = state
        if existed:
            overrides[key] = value
        else:
            overrides.pop(key, None)
    return overrides


def _descriptor_request_overrides(
    descriptor: dict[str, Any],
    *,
    api_mode: str = "",
) -> dict[str, Any]:
    """Build the trusted per-model request layer for the active selection.

    Dovie's LLM proxy accepts ``reasoning_effort`` as a stable product-level
    selector on the Chat Completions route.  Responses requests instead get
    their canonical ``reasoning.effort`` object from ``agent.reasoning_config``;
    forwarding the Chat alias as well creates an invalid mixed-protocol body.
    The catalog's ``request_params`` contains other registry-approved top-level
    parameters. Core chat identity fields remain owned by Hermes and are never
    accepted from the descriptor.
    """
    request_params = descriptor.get("request_params")
    overrides = dict(request_params) if isinstance(request_params, dict) else {}
    for key in _RESERVED_REQUEST_PARAM_KEYS:
        overrides.pop(key, None)

    resolved_api_mode = (
        str(api_mode or model_descriptor_api_mode(descriptor)).strip().lower()
    )
    if resolved_api_mode == "codex_responses":
        overrides.pop("reasoning_effort", None)
        return overrides

    effort = str(descriptor.get("reasoning_effort") or "").strip().lower()
    supported = descriptor.get("reasoning_efforts")
    if effort and (
        not isinstance(supported, list)
        or not supported
        or effort in supported
    ):
        overrides["reasoning_effort"] = effort
    elif effort:
        # Never fall back to a catalog default after the client supplied an
        # invalid selection; let the proxy/model default apply instead.
        overrides.pop("reasoning_effort", None)
    elif descriptor.get("reasoning_enabled") is False:
        overrides.pop("reasoning_effort", None)
    return overrides


def _apply_descriptor_request_overrides(agent: Any, descriptor: dict[str, Any]) -> None:
    if agent is None:
        return
    overrides = _restore_descriptor_request_overrides(agent)
    api_mode = str(getattr(agent, "api_mode", "") or "").strip().lower()
    descriptor_overrides = _descriptor_request_overrides(
        descriptor,
        api_mode=api_mode,
    )
    masked_keys: set[str] = set()
    if api_mode == "codex_responses":
        # A previous Chat model or session setting may have left the alias in
        # request_overrides. Shadow it while Responses owns the wire contract,
        # then restore it if the descriptor is later cleared or switched back.
        masked_keys.add("reasoning_effort")
    shadowed: dict[str, tuple[bool, Any]] = {}
    for key in descriptor_overrides.keys() | masked_keys:
        shadowed[key] = (key in overrides, overrides.get(key))
        if key in descriptor_overrides:
            overrides[key] = descriptor_overrides[key]
        else:
            overrides.pop(key, None)
    setattr(agent, "request_overrides", overrides)
    setattr(agent, "_model_descriptor_request_override_shadow", shadowed)


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
        "default_reasoning_effort",
        "reasoning_effort",
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

    reasoning_efforts = raw.get("reasoning_efforts")
    if isinstance(reasoning_efforts, list):
        normalized_efforts: list[str] = []
        for value in reasoning_efforts:
            effort = str(value or "").strip().lower()
            if effort and effort not in normalized_efforts:
                normalized_efforts.append(effort)
        descriptor["reasoning_efforts"] = normalized_efforts

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
            _apply_descriptor_api_mode(agent, normalized)
            _apply_descriptor_context_window(agent, normalized.get("context_window"))
            _apply_descriptor_reasoning_config(agent, normalized)
            _apply_descriptor_request_overrides(agent, normalized)
            if "reasoning_enabled" in normalized or "reasoning_effort" in normalized:
                session["create_reasoning_override"] = getattr(
                    agent, "reasoning_config", None
                )
        return
    if clear_if_empty:
        session.pop("model_descriptor", None)
        if agent is not None:
            _restore_descriptor_api_mode(agent)
            if hasattr(agent, "model_descriptor"):
                setattr(agent, "model_descriptor", {})
            setattr(
                agent,
                "request_overrides",
                _restore_descriptor_request_overrides(agent),
            )
            setattr(agent, "_model_descriptor_request_override_shadow", {})


def bind_session_agent(session: dict[str, Any], agent: Any) -> None:
    """Bind a newly built agent and replay pending session model semantics.

    Agent creation is intentionally deferred.  Model descriptors can therefore
    arrive before an agent exists; every lifecycle path that installs a fresh
    agent must replay the descriptor before the first model request.
    """
    session["agent"] = agent
    descriptor = session.get("model_descriptor")
    if isinstance(descriptor, dict) and descriptor:
        set_session_model_descriptor(session, descriptor)
