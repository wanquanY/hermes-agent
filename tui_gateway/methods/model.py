# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


def _model_set_value(params: dict) -> str:
    value = params.get("model", params.get("value", ""))
    return str(value or "").strip()


@method("model.set")
def _(rid, params: dict) -> dict:
    raw_selection = params.get("selection") or params.get("model_selection")
    if isinstance(raw_selection, dict):
        try:
            from hermes_cli.model_routes import normalize_model_selection
            from tui_gateway.services.model_route_runtime import (
                persist_selection,
                route_error_response,
                route_snapshot,
                selection_from_state,
            )

            selection = normalize_model_selection(raw_selection)
            value = selection["model_id"]
            execution_target = params.get("execution_target") or params.get(
                "executionTarget"
            )
            if not isinstance(execution_target, dict):
                execution_target = {"kind": "conversation"}
            scoped_member_selection = (
                str(execution_target.get("kind") or "") == "team_member"
            )
            session_id = str(params.get("session_id") or "").strip()
            session = _sessions.get(session_id)
            if session and session.get("running"):
                return _err(
                    rid,
                    4009,
                    "session busy — /interrupt the current turn before switching models",
                )
            conversation_session_id = str(
                params.get("conversation_session_id")
                or params.get("conversationSessionId")
                or (session or {}).get("session_key")
                or session_id
            ).strip()
            db = _get_db()
            current_selection = None
            current_revision = 0
            try:
                current_selection, current_revision = selection_from_state(
                    conversation_session_id=conversation_session_id,
                    live_session=session,
                    db=db,
                    execution_target=execution_target,
                )
            except Exception:
                current_selection = None
            snapshot = route_snapshot(selection)
            changed = current_selection != snapshot["selection"]

            descriptor = _normalize_model_descriptor(
                params.get("model_descriptor") or params.get("modelDescriptor")
            )
            if descriptor and descriptor["id"] != value:
                return _err(
                    rid,
                    4002,
                    "model descriptor id must match selection model_id",
                )
            if (
                changed
                and not scoped_member_selection
                and session
                and session.get("agent") is not None
            ):
                result = _apply_model_switch(
                    session_id,
                    session,
                    value,
                    parsed_flags=(
                        value,
                        selection["provider_id"],
                        False,
                        False,
                        True,
                    ),
                    catalog_model_id=(
                        str(selection.get("catalog_model_id") or "")
                        or _authoritative_catalog_model_id(descriptor)
                    ),
                    managed_selection=snapshot["selection"],
                )
                warning = result.get("warning") or ""
            else:
                warning = ""
            next_revision = current_revision + 1 if changed else max(
                current_revision,
                1,
            )
            if session is not None:
                if scoped_member_selection:
                    member_id = str(
                        execution_target.get("target_member_id")
                        or execution_target.get("targetMemberId")
                        or ""
                    ).strip()
                    scoped = dict(session.get("model_selections") or {})
                    scoped[f"team_member:{member_id}"] = {
                        "selection": dict(snapshot["selection"]),
                        "revision": next_revision,
                        "route_snapshot": dict(snapshot),
                    }
                    session["model_selections"] = scoped
                else:
                    session["model_selection"] = dict(snapshot["selection"])
                    session["model_selection_revision"] = next_revision
                    session["route_snapshot"] = dict(snapshot)
                    _set_session_model_descriptor(
                        session,
                        descriptor,
                        clear_if_empty=True,
                    )
            persist_selection(
                db=db,
                conversation_session_id=conversation_session_id,
                selection=snapshot["selection"],
                snapshot=snapshot,
                revision=next_revision,
                execution_target=execution_target,
            )
            if changed:
                _emit(
                    "session.model.changed",
                    session_id or conversation_session_id,
                    {
                        **snapshot,
                        "conversation_session_id": conversation_session_id,
                        "session_revision": next_revision,
                        "execution_target": execution_target,
                    },
                )
            return _ok(
                rid,
                {
                    **snapshot,
                    "scope": "session",
                    "changed": changed,
                    "session_revision": next_revision,
                    "warning": warning or None,
                },
            )
        except Exception as error:
            from tui_gateway.services.model_route_runtime import route_error_response

            response = route_error_response(rid, error)
            return response or _err(rid, 5001, str(error))

    value = _model_set_value(params)
    if not value:
        return _err(rid, 4002, "model value required")

    session_id = params.get("session_id", "")
    session = _sessions.get(session_id)
    try:
        descriptor = _normalize_model_descriptor(
            params.get("model_descriptor") or params.get("modelDescriptor")
        )
        if descriptor and descriptor["id"] != value:
            return _err(
                rid,
                4002,
                "model descriptor id must match model value",
            )
        catalog_model_id = _authoritative_catalog_model_id(descriptor)
        if session:
            # Keep model mutation outside in-flight turns. agent.switch_model()
            # mutates provider/model/client state that run_conversation reads.
            if session.get("running"):
                return _err(
                    rid,
                    4009,
                    "session busy — /interrupt the current turn before switching models",
                )
            switch_options = {
                # ``model.set`` addresses one explicit runtime session.  A UI
                # selection must never rewrite the profile-global model just
                # because the CLI's interactive /model default is persistent.
                "parsed_flags": (value, "", False, False, True),
            }
            if catalog_model_id:
                switch_options["catalog_model_id"] = catalog_model_id
            result = _apply_model_switch(
                session_id,
                session,
                value,
                **switch_options,
            )
            _set_session_model_descriptor(
                session,
                descriptor,
                clear_if_empty=True,
            )
        else:
            if catalog_model_id:
                result = _apply_model_switch(
                    "",
                    {"agent": None},
                    value,
                    catalog_model_id=catalog_model_id,
                )
            else:
                result = _apply_model_switch("", {"agent": None}, value)
        return _ok(
            rid,
            {"key": "model", "value": result["value"], "warning": result["warning"]},
        )
    except Exception as e:
        return _err(rid, 5001, str(e))


@method("model.options")
def _(rid, params: dict) -> dict:
    try:
        from hermes_cli.inventory import build_models_payload, load_picker_context

        session = _sessions.get(params.get("session_id", ""))
        agent = session.get("agent") if session else None
        # Layer agent-session state on top of disk config — once an agent
        # is spawned, IT owns the live provider/model/base_url. Empty
        # agent attributes must NOT clobber disk config (with_overrides
        # is truthy-only).
        ctx = load_picker_context().with_overrides(
            current_provider=getattr(agent, "provider", "") if agent else "",
            current_model=(
                (getattr(agent, "model", "") if agent else "") or _resolve_model()
            ),
            current_base_url=getattr(agent, "base_url", "") if agent else "",
        )
        # picker_hints + canonical_order produce the TUI's required shape:
        # `authenticated`/`auth_type`/`key_env`/`warning` per row, in
        # CANONICAL_PROVIDERS declaration order. include_unconfigured=True
        # so the picker can show the full provider universe (with the
        # setup-hint warning attached) instead of only authed rows.
        # Curated model lists are preserved — list_authenticated_providers
        # populates `models` from the curated catalog, not provider_model_ids
        # (which would pull non-agentic models like TTS/embeddings/etc.).
        payload = build_models_payload(
            ctx,
            include_unconfigured=True,
            picker_hints=True,
            canonical_order=True,
            max_models=50,
        )
        from hermes_cli.model_connection_inventory import (
            project_model_options_v2,
            runtime_connection_repository,
            runtime_credential_status_reader,
        )

        repository = runtime_connection_repository()
        if repository is not None:
            payload = project_model_options_v2(
                payload,
                repository,
                credential_status_reader=runtime_credential_status_reader(),
            )
        return _ok(rid, payload)
    except Exception as e:
        return _err(rid, 5033, str(e))


@method("model.save_key")
def _(rid, params: dict) -> dict:
    """Save an API key for a provider, then return its refreshed model list.

    Params:
        slug: provider slug (e.g. "deepseek", "xai")
        api_key: the key value to save

    Returns the provider dict with models populated (same shape as
    model.options entries) on success.
    """
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
        from hermes_cli.config import is_managed
        from hermes_cli.credential_lifecycle import save_provider_env_credential
        from hermes_cli.inventory import build_models_payload, load_picker_context

        slug = (params.get("slug") or "").strip()
        api_key = (params.get("api_key") or "").strip()
        if not slug or not api_key:
            return _err(rid, 4001, "slug and api_key are required")

        if is_managed():
            return _err(rid, 4006, "managed install — credentials are read-only")

        pconfig = PROVIDER_REGISTRY.get(slug)
        if not pconfig:
            return _err(rid, 4002, f"unknown provider: {slug}")
        if pconfig.auth_type != "api_key":
            return _err(
                rid,
                4003,
                f"{pconfig.name} uses {pconfig.auth_type} auth — "
                f"run `hermes model` to configure",
            )
        if not pconfig.api_key_env_vars:
            return _err(rid, 4004, f"no env var defined for {pconfig.name}")

        # Save the key to ~/.hermes/.env
        env_var = pconfig.api_key_env_vars[0]
        save_provider_env_credential(env_var, api_key)
        # Also set in current process so the refreshed inventory sees it.
        import os

        os.environ[env_var] = api_key

        # Refresh provider data via the shared inventory builder so this
        # surface stays in lock-step with model.options + dashboard
        # /api/model/options. picker_hints=True ensures the returned row
        # carries `authenticated` for the TUI frontend.
        session = _sessions.get(params.get("session_id", ""))
        agent = session.get("agent") if session else None
        ctx = load_picker_context().with_overrides(
            current_provider=getattr(agent, "provider", "") if agent else "",
            current_model=(
                (getattr(agent, "model", "") if agent else "") or _resolve_model()
            ),
            current_base_url=getattr(agent, "base_url", "") if agent else "",
        )
        payload = build_models_payload(
            ctx, picker_hints=True, max_models=50,
        )
        provider_data = next(
            (p for p in payload["providers"] if p["slug"] == slug), None
        )
        if provider_data is None:
            # Key was saved but provider didn't appear — still return success.
            provider_data = {
                "slug": slug,
                "name": pconfig.name,
                "is_current": False,
                "models": [],
                "total_models": 0,
                "authenticated": True,
            }
        # picker_hints sets `authenticated` from the row state, but the
        # synthetic fallback above doesn't go through that path.
        provider_data["authenticated"] = True
        return _ok(rid, {"provider": provider_data})
    except Exception as e:
        return _err(rid, 5034, str(e))


@method("model.disconnect")
def _(rid, params: dict) -> dict:
    """Remove credentials for a provider.

    Params:
        slug: provider slug (e.g. "deepseek", "xai")

    Returns success status and the provider's slug.
    """
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY, clear_provider_auth
        from hermes_cli.credential_lifecycle import remove_provider_env_credential

        slug = (params.get("slug") or "").strip()
        if not slug:
            return _err(rid, 4001, "slug is required")

        pconfig = PROVIDER_REGISTRY.get(slug)
        cleared_env = False
        cleared_auth = False

        # Remove API key env vars from .env and process
        if pconfig and pconfig.api_key_env_vars:
            for ev in pconfig.api_key_env_vars:
                if remove_provider_env_credential(ev).get("found"):
                    cleared_env = True

        # Clear OAuth / credential pool state
        cleared_auth = clear_provider_auth(slug)

        if not cleared_env and not cleared_auth:
            return _err(rid, 4005, f"no credentials found for {slug}")

        provider_name = pconfig.name if pconfig else slug
        return _ok(
            rid,
            {
                "slug": slug,
                "name": provider_name,
                "disconnected": True,
            },
        )
    except Exception as e:
        return _err(rid, 5035, str(e))
