"""Runtime credential rebinding for warm TUI gateway agents."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def remember_requested_runtime_provider(
    agent: Any,
    runtime: dict[str, Any],
    requested_provider: str | None,
) -> None:
    agent._gateway_runtime_requested_provider = (
        runtime.get("requested_provider") or requested_provider
    )


def ensure_agent_runtime_current(
    *,
    sid: str,
    session: dict[str, Any],
    resolve_model: Callable[[], str],
    emit_session_info: Callable[[str, Any], None],
) -> bool:
    """Refresh an existing TUI agent's runtime credentials before a turn."""
    agent = session.get("agent") if isinstance(session, dict) else None
    if agent is None:
        return False

    # Codex-employee guard: codex_app_server agents are backed by a codex CLI
    # subprocess that authenticates itself via CODEX_HOME/auth.json (BYO) or
    # CODEX_HOME/config.toml `[model_providers.doxie]` (platform). There are
    # no hermes-side credentials to "keep current". Re-resolving here would
    # rebuild the runtime from the composer's `_gateway_runtime_requested_
    # provider` (typically `custom`, tied to whichever model the composer
    # has selected) — that yields chat_completions against the platform
    # gateway and silently swaps the codex CLI out from under the session.
    # Symptom: user chatting with a Codex employee gets replies from
    # glm-5.2 / claude / etc via the gateway, never touching their ChatGPT
    # account. Skip the rebind entirely for codex_app_server sessions.
    if str(getattr(agent, "api_mode", "") or "").strip() == "codex_app_server":
        return False

    requested_provider = str(
        getattr(agent, "_gateway_runtime_requested_provider", "") or ""
    ).strip() or None
    if requested_provider is None:
        return False

    from hermes_cli.runtime_provider import resolve_runtime_provider

    model = str(getattr(agent, "model", "") or resolve_model()).strip()
    runtime = resolve_runtime_provider(
        requested=requested_provider,
        target_model=model or None,
    )
    next_provider = runtime.get("provider")
    next_base_url = runtime.get("base_url")
    next_api_key = runtime.get("api_key")
    next_api_mode = runtime.get("api_mode")
    changed = (
        getattr(agent, "provider", None) != next_provider
        or getattr(agent, "base_url", None) != next_base_url
        or getattr(agent, "api_key", None) != next_api_key
        or getattr(agent, "api_mode", None) != next_api_mode
    )
    if not changed:
        return False

    switch_model = getattr(agent, "switch_model", None)
    if callable(switch_model):
        switch_model(
            new_model=model,
            new_provider=next_provider,
            api_key=next_api_key,
            base_url=next_base_url,
            api_mode=next_api_mode,
        )
    else:
        agent.model = model
        agent.provider = next_provider
        agent.base_url = next_base_url
        agent.api_key = next_api_key
        agent.api_mode = next_api_mode

    remember_requested_runtime_provider(agent, runtime, requested_provider)
    if runtime.get("credential_pool") is not None:
        agent._credential_pool = runtime.get("credential_pool")
    emit_session_info(sid, agent)
    return True
