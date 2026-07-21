"""
Session-scoped context variables for the Hermes gateway.

Most session state (``HERMES_SESSION_PLATFORM``,
``HERMES_SESSION_CHAT_ID``, etc.) is stored in Python
``contextvars.ContextVar`` values instead of process-global
``os.environ``.

**Why this matters**

The gateway processes messages concurrently via ``asyncio``.  When two
messages arrive at the same time the old code did:

    os.environ["HERMES_SESSION_THREAD_ID"] = str(context.source.thread_id)

Because ``os.environ`` is *process-global*, Message A's value was
silently overwritten by Message B before Message A's agent finished
running.  Background-task notifications and tool calls therefore routed
to the wrong thread.

``contextvars.ContextVar`` values are *task-local*: each ``asyncio``
task (and any ``run_in_executor`` thread it spawns) gets its own copy,
so concurrent messages never interfere.

``HERMES_DOVIE_PRODUCT_CONTEXT`` is the deliberate exception.  Dovie
attribution is turn-local inside a worker subprocess, and that subprocess
is guarded to run only one turn at a time.  The agent code frequently
creates raw ``threading.Thread`` instances; those threads do not inherit
ContextVars.  Dovie therefore uses ``os.environ`` as a process-level
side channel so every nested thread in the same worker can read the same
turn context without additional propagation glue.  ``set_session_vars``
snapshots and restores the previous environment value on clear.

**Backward compatibility**

The public helper ``get_session_env(name, default="")`` mirrors the old
``os.getenv("HERMES_SESSION_*", ...)`` calls.  Existing tool code only
needs to replace the import + call site:

    # before
    import os
    platform = os.getenv("HERMES_SESSION_PLATFORM", "")

    # after
    from channels.session_context import get_session_env
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
"""

import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

# Sentinel to distinguish "never set in this context" from "explicitly set to empty".
# When a contextvar holds _UNSET, we fall back to os.environ (CLI/cron compat).
# When it holds "" (after clear_session_vars resets it), we return "" — no fallback.
_UNSET: Any = object()

# ---------------------------------------------------------------------------
# Per-task session variables
# ---------------------------------------------------------------------------

_SESSION_PLATFORM: ContextVar = ContextVar("HERMES_SESSION_PLATFORM", default=_UNSET)
_SESSION_CHAT_ID: ContextVar = ContextVar("HERMES_SESSION_CHAT_ID", default=_UNSET)
_SESSION_CHAT_NAME: ContextVar = ContextVar("HERMES_SESSION_CHAT_NAME", default=_UNSET)
_SESSION_THREAD_ID: ContextVar = ContextVar("HERMES_SESSION_THREAD_ID", default=_UNSET)
_SESSION_USER_ID: ContextVar = ContextVar("HERMES_SESSION_USER_ID", default=_UNSET)
_SESSION_USER_NAME: ContextVar = ContextVar("HERMES_SESSION_USER_NAME", default=_UNSET)
_SESSION_KEY: ContextVar = ContextVar("HERMES_SESSION_KEY", default=_UNSET)
_SESSION_ID: ContextVar = ContextVar("HERMES_SESSION_ID", default=_UNSET)
_DOVIE_BROWSER_SESSION_ID: ContextVar = ContextVar("DOVIE_BROWSER_SESSION_ID", default=_UNSET)
_TERMINAL_CWD: ContextVar = ContextVar("TERMINAL_CWD", default=_UNSET)
# ID of the message that triggered the current turn. Used as a reply anchor
# so background-process notifications stay inside the originating Telegram
# private-chat topic (those lanes route only with thread id + reply anchor).
_SESSION_MESSAGE_ID: ContextVar = ContextVar("HERMES_SESSION_MESSAGE_ID", default=_UNSET)

# Whether the current session's delivery channel can route an ASYNC completion
# back to the agent AFTER the current turn ends (i.e. wake a fresh turn).
#
# True  — CLI (in-process completion_queue drain) and the real gateway
#         platforms (Telegram/Discord/Slack/...), which hold a persistent
#         outbound channel and run the watcher/drain loops.
# False — stateless request/response adapters (the API server: every route,
#         spec and proprietary, tears down its channel when the turn ends, so
#         a background completion that finishes later has nowhere to go).
#
# Tools that promise async delivery (terminal notify_on_complete /
# watch_patterns, delegate_task background=True) read this via
# ``async_delivery_supported()`` and refuse to hand out a promise the channel
# can't keep — turning a silent no-op into an explicit contract.
#
# Default _UNSET => treated as supported, so CLI (which never sets a platform)
# and any contextvar-unaware path keep working. Stateless adapters opt OUT by
# setting ``supports_async_delivery = False`` on the adapter class; the gateway
# propagates that into this contextvar at session-bind time.
_SESSION_ASYNC_DELIVERY: ContextVar = ContextVar("HERMES_SESSION_ASYNC_DELIVERY", default=_UNSET)

# Cron auto-delivery vars — set per-job in run_job() so concurrent jobs
# don't clobber each other's delivery targets.
_CRON_AUTO_DELIVER_PLATFORM: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_PLATFORM", default=_UNSET)
_CRON_AUTO_DELIVER_CHAT_ID: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_CHAT_ID", default=_UNSET)
_CRON_AUTO_DELIVER_THREAD_ID: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_THREAD_ID", default=_UNSET)

_DOVIE_PRODUCT_CONTEXT_ENV = "HERMES_DOVIE_PRODUCT_CONTEXT"


@dataclass(frozen=True)
class _DovieEnvSnapshot:
    existed: bool
    value: str


_VAR_MAP = {
    "HERMES_SESSION_PLATFORM": _SESSION_PLATFORM,
    "HERMES_SESSION_CHAT_ID": _SESSION_CHAT_ID,
    "HERMES_SESSION_CHAT_NAME": _SESSION_CHAT_NAME,
    "HERMES_SESSION_THREAD_ID": _SESSION_THREAD_ID,
    "HERMES_SESSION_USER_ID": _SESSION_USER_ID,
    "HERMES_SESSION_USER_NAME": _SESSION_USER_NAME,
    "HERMES_SESSION_KEY": _SESSION_KEY,
    "HERMES_SESSION_ID": _SESSION_ID,
    "DOVIE_BROWSER_SESSION_ID": _DOVIE_BROWSER_SESSION_ID,
    "TERMINAL_CWD": _TERMINAL_CWD,
    "HERMES_SESSION_MESSAGE_ID": _SESSION_MESSAGE_ID,
    "HERMES_CRON_AUTO_DELIVER_PLATFORM": _CRON_AUTO_DELIVER_PLATFORM,
    "HERMES_CRON_AUTO_DELIVER_CHAT_ID": _CRON_AUTO_DELIVER_CHAT_ID,
    "HERMES_CRON_AUTO_DELIVER_THREAD_ID": _CRON_AUTO_DELIVER_THREAD_ID,
}


def set_session_vars(
    platform: str = "",
    chat_id: str = "",
    chat_name: str = "",
    thread_id: str = "",
    user_id: str = "",
    user_name: str = "",
    session_key: str = "",
    terminal_cwd: str = "",
    dovie_product_context: str = "",
    dovie_browser_session_id: str = "",
    session_id: str = "",
    message_id: str = "",
    cwd: str = "",
    async_delivery: bool = True,
) -> list:
    """Set all session context variables and return reset tokens.

    Call ``clear_session_vars(tokens)`` in a ``finally`` block to restore
    the previous values when the handler exits.

    Returns a list of ``Token`` objects (one per variable) that can be
    passed to ``clear_session_vars``.

    ``async_delivery`` declares whether this session's channel can route a
    background completion back to the agent after the turn ends. Stateless
    request/response adapters (the API server) pass ``False``.
    """
    dovie_snapshot = _DovieEnvSnapshot(
        existed=_DOVIE_PRODUCT_CONTEXT_ENV in os.environ,
        value=os.environ.get(_DOVIE_PRODUCT_CONTEXT_ENV, ""),
    )
    os.environ[_DOVIE_PRODUCT_CONTEXT_ENV] = dovie_product_context or ""
    tokens = [
        _SESSION_PLATFORM.set(platform),
        _SESSION_CHAT_ID.set(chat_id),
        _SESSION_CHAT_NAME.set(chat_name),
        _SESSION_THREAD_ID.set(thread_id),
        _SESSION_USER_ID.set(user_id),
        _SESSION_USER_NAME.set(user_name),
        _SESSION_KEY.set(session_key),
        _DOVIE_BROWSER_SESSION_ID.set(dovie_browser_session_id),
        _TERMINAL_CWD.set(terminal_cwd),
        _SESSION_ID.set(session_id),
        _SESSION_MESSAGE_ID.set(message_id),
        _SESSION_ASYNC_DELIVERY.set(bool(async_delivery)),
        dovie_snapshot,
    ]
    return tokens


def clear_session_vars(tokens: list) -> None:
    """Mark session context variables as explicitly cleared.

    Sets all variables to ``""`` so that ``get_session_env`` returns an empty
    string instead of falling back to (potentially stale) ``os.environ``
    values.  The *tokens* argument is accepted for API compatibility with
    callers that saved the return value of ``set_session_vars``, but the
    actual clearing uses ``var.set("")`` rather than ``var.reset(token)``
    to ensure the "explicitly cleared" state is distinguishable from
    "never set" (which holds the ``_UNSET`` sentinel).
    """
    for var in (
        _SESSION_PLATFORM,
        _SESSION_CHAT_ID,
        _SESSION_CHAT_NAME,
        _SESSION_THREAD_ID,
        _SESSION_USER_ID,
        _SESSION_USER_NAME,
        _SESSION_KEY,
        _DOVIE_BROWSER_SESSION_ID,
        _TERMINAL_CWD,
        _SESSION_ID,
        _SESSION_MESSAGE_ID,
    ):
        var.set("")
    dovie_snapshot = next(
        (
            token
            for token in tokens or []
            if isinstance(token, _DovieEnvSnapshot)
        ),
        None,
    )
    if dovie_snapshot is not None:
        if dovie_snapshot.existed:
            os.environ[_DOVIE_PRODUCT_CONTEXT_ENV] = dovie_snapshot.value
        else:
            os.environ.pop(_DOVIE_PRODUCT_CONTEXT_ENV, None)
    # Reset async-delivery capability to the "never set" sentinel rather than a
    # falsy value: a cleared context should fall back to the default-supported
    # behavior (CLI / unaware paths), not be mistaken for an opted-out
    # stateless adapter.
    _SESSION_ASYNC_DELIVERY.set(_UNSET)
    try:
        from agent.runtime_cwd import clear_session_cwd

        clear_session_cwd()
    except Exception:
        pass


def get_session_env(name: str, default: str = "") -> str:
    """Read a session context variable by its legacy ``HERMES_SESSION_*`` name.

    Drop-in replacement for ``os.getenv("HERMES_SESSION_*", default)``.

    Resolution order:
    1. Context variable (set by the gateway for concurrency-safe access).
       If the variable was explicitly set (even to ``""``) via
       ``set_session_vars`` or ``clear_session_vars``, that value is
       returned — **no fallback to os.environ**.
    2. ``os.environ`` (only when the context variable was never set in
       this context — i.e. CLI, cron scheduler, and test processes that
       don't use ``set_session_vars`` at all).
    3. *default*
    """
    if name == _DOVIE_PRODUCT_CONTEXT_ENV:
        return os.environ.get(name, default)

    var = _VAR_MAP.get(name)
    if var is not None:
        value = var.get()
        if value is not _UNSET:
            return value
    # Fall back to os.environ for CLI, cron, and test compatibility
    return os.getenv(name, default)


def get_session_context_env(name: str, default: str = "") -> str:
    """Read session context without falling back to process-global env vars."""
    var = _VAR_MAP.get(name)
    if var is None:
        return default
    value = var.get()
    if value is _UNSET:
        return default
    return value


def async_delivery_supported() -> bool:
    """Whether the current session can deliver a background completion later.

    Returns ``False`` only when the active session was explicitly bound by a
    stateless adapter (the API server) that cannot route a notification back to
    the agent after the turn ends. CLI, cron, and the real gateway platforms —
    and any path that never bound the contextvar — return ``True``.
    """
    value = _SESSION_ASYNC_DELIVERY.get()
    if value is _UNSET:
        return True
    return bool(value)


def declare_stateless_channel():
    """Disable detached delivery without engaging the full session context.

    Returns the ContextVar token so in-process callers and tests can restore
    their prior capability after the one-shot scope ends.
    """
    return _SESSION_ASYNC_DELIVERY.set(False)


def restore_async_delivery_capability(token) -> None:
    """Restore a token returned by :func:`declare_stateless_channel`."""
    _SESSION_ASYNC_DELIVERY.reset(token)
