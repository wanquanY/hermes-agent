"""Stateless auxiliary LLM JSON-RPC methods."""

from __future__ import annotations

from typing import Any

from agent.aux_accounting import reset_accounting_context, set_accounting_context
from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


def _main_runtime_from_agent(agent: Any) -> dict[str, Any] | None:
    if agent is None:
        return None
    current_runtime = getattr(agent, "_current_main_runtime", None)
    if callable(current_runtime):
        runtime = current_runtime()
        if isinstance(runtime, dict):
            return runtime or None

    runtime: dict[str, Any] = {}
    for field in ("provider", "model", "base_url", "api_key", "api_mode", "auth_mode"):
        value = getattr(agent, field, None)
        if isinstance(value, str) and value.strip():
            runtime[field] = value.strip()
        elif field == "api_key" and callable(value):
            runtime[field] = value
    return runtime or None


@method("llm.oneshot")
def llm_oneshot(rid, params: dict) -> dict:
    """Run a model helper without mutating any session transcript."""
    template = str(params.get("template") or "").strip() or None
    instructions = params.get("instructions") or ""
    user_input = params.get("input") or ""
    variables = params.get("variables")
    if not isinstance(variables, dict):
        variables = {}
    task = str(params.get("task") or "title_generation").strip() or "title_generation"

    try:
        max_tokens = int(params.get("max_tokens") or 1024)
    except (TypeError, ValueError):
        max_tokens = 1024
    try:
        temperature = float(params["temperature"]) if params.get("temperature") is not None else 0.3
    except (TypeError, ValueError):
        temperature = 0.3

    if not template and not str(instructions).strip() and not str(user_input).strip():
        return _err(rid, 4030, "llm.oneshot requires a template or instructions/input")

    session = _sessions.get(str(params.get("session_id") or ""))
    agent = session.get("agent") if isinstance(session, dict) else None
    token = set_accounting_context(agent=agent) if agent is not None else None
    try:
        from agent.oneshot import run_oneshot

        text = run_oneshot(
            instructions=instructions,
            user_input=user_input,
            template=template,
            variables=variables,
            task=task,
            max_tokens=max_tokens,
            temperature=temperature,
            main_runtime=_main_runtime_from_agent(agent),
        )
    except KeyError as exc:
        return _err(rid, 4031, str(exc))
    except ValueError as exc:
        return _err(rid, 4032, str(exc))
    except Exception as exc:
        logger.warning("llm.oneshot failed: %s", exc)
        return _err(rid, 5030, f"one-shot generation failed: {exc}")
    finally:
        if token is not None:
            reset_accounting_context(token)

    return _ok(rid, {"text": text})
