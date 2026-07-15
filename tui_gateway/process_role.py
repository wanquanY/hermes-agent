"""R1: architecture-level single-writer guard for ``run_events``.

Phase 8b unified the control-plane state.db across main and worker
processes: both open the same physical file. Historically the worker
also called ``append_run_event`` from inside its own agent thread —
racing the main sidecar's ``WorkerFrameRouter`` persist and producing
duplicate rows (one per writer) with divergent payloads (worker copy
missing ``run_context`` / activity / participant stamping, since
``_emit`` never has a ``RunContext``). ``duplicate_terminal`` only
tolerated TERMINAL events; non-terminal streams accumulated silently.

The correct architectural invariant is:

    Worker = event source (produces frames, ships them over stdout).
    Main   = single writer (receives stdout frames, persists ONCE via
             ``WorkerFrameRouter._publish_event_with_db``).

This module carries the process-role bit that lets ``record_event``
enforce that invariant at the write site, so no ad-hoc monkey-patches
in ``worker_publish_bridge`` or per-code-path patches are needed — any
future worker (member-chat, node worker, activity_reconciler, whatever)
inherits the guard automatically.

Default: ``IS_WORKER_PROCESS = False`` (safe for main sidecar and every
test that hasn't explicitly opted in). ``run_worker._main_async`` flips
it to ``True`` on entry, once per worker process. Do NOT flip it back —
the setting is per-process and lasts for the process's lifetime.
"""

from __future__ import annotations

IS_WORKER_PROCESS: bool = False


def mark_as_worker_process() -> None:
    """Called exactly once by ``run_worker._main_async`` at process
    startup. Idempotent — repeat calls are no-ops."""
    global IS_WORKER_PROCESS
    IS_WORKER_PROCESS = True


def is_worker_process() -> bool:
    """Read the current process role. Kept as a function (not just the
    global) so tests can monkeypatch the module attribute without
    fighting a cached local binding."""
    return IS_WORKER_PROCESS
