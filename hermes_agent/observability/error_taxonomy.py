"""Error taxonomy — RecoverableError / FatalError + drop/degrade/fallback (spec §10.2).

Every ``try/except`` must classify its exception as either recoverable (log
warning + continue) or fatal (log exception + escalate). Silent swallow
(``except: pass`` / ``except Exception: pass``) is banned by the AST guard
in ``tests/observability/test_no_silent_swallow.py``.

Three canonical observability events (spec §10.2):

    log_drop(reason, code, **ctx)      # event=drop     — an input/frame was intentionally discarded
    log_degrade(from_state, to_state, cause, **ctx)  # event=degrade — capability downgraded, still functional
    log_fallback(path, capability_missing, **ctx)    # event=fallback — using compat path because feature absent
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

DROP_TAG = "drop"
DEGRADE_TAG = "degrade"
FALLBACK_TAG = "fallback"


class RecoverableError(Exception):
    """Callers should log warning + continue on the fallback path.

    Examples:
        * ``SeqAllocatorBusy`` after bounded retries → degrade path
        * transient IO error on optional persistence
        * upstream 5xx with retry-after semantics

    ``except RecoverableError`` is the ONLY spec-approved swallow form.
    """


class FatalError(Exception):
    """Callers should log.exception() and re-raise / escalate.

    Examples:
        * schema drift detected at startup
        * invariant violation (idempotency broken)
        * unrecoverable storage corruption
    """


@dataclass(frozen=True)
class ObservabilityEvent:
    """Structured envelope for drop/degrade/fallback log lines."""

    event: str          # 'drop' | 'degrade' | 'fallback'
    fields: dict[str, Any]


def log_drop(
    logger: logging.Logger,
    *,
    reason: str,
    code: str = "",
    **context: Any,
) -> ObservabilityEvent:
    """Emit an ``event=drop`` line. Reason MUST be human-readable + machine-classifiable."""
    fields = {"event": DROP_TAG, "reason": str(reason), "code": str(code), **context}
    logger.warning("event=drop reason=%s code=%s %s", reason, code, _fmt_ctx(context))
    return ObservabilityEvent(event=DROP_TAG, fields=fields)


def log_degrade(
    logger: logging.Logger,
    *,
    from_state: str,
    to_state: str,
    cause: str,
    **context: Any,
) -> ObservabilityEvent:
    """Emit an ``event=degrade`` line. E.g. WAL→DELETE journal, canonical→raw."""
    fields = {
        "event": DEGRADE_TAG,
        "from": str(from_state),
        "to": str(to_state),
        "cause": str(cause),
        **context,
    }
    logger.warning(
        "event=degrade from=%s to=%s cause=%s %s",
        from_state,
        to_state,
        cause,
        _fmt_ctx(context),
    )
    return ObservabilityEvent(event=DEGRADE_TAG, fields=fields)


def log_fallback(
    logger: logging.Logger,
    *,
    path: str,
    capability_missing: str,
    **context: Any,
) -> ObservabilityEvent:
    """Emit an ``event=fallback`` line. Used when a capability is absent
    and code takes a legacy branch (Phase H4 handshake gating).
    """
    fields = {
        "event": FALLBACK_TAG,
        "path": str(path),
        "capability_missing": str(capability_missing),
        **context,
    }
    logger.info(
        "event=fallback path=%s capability_missing=%s %s",
        path,
        capability_missing,
        _fmt_ctx(context),
    )
    return ObservabilityEvent(event=FALLBACK_TAG, fields=fields)


def _fmt_ctx(ctx: dict[str, Any]) -> str:
    if not ctx:
        return ""
    return " ".join(f"{k}={v!r}" for k, v in sorted(ctx.items()))
