"""Run-domain gateway methods (spec §4.2, §J8)."""

from __future__ import annotations

from typing import Any

from hermes_agent.domain.run_terminator import TerminateCause
from hermes_agent.gateway.auth import requires_permission
from hermes_agent.gateway.error_codes import ErrorCode, MethodError
from hermes_agent.gateway.pipeline import DispatchContext
from hermes_agent.gateway.registry import MethodRegistry
from hermes_agent.orchestration import RunLaunchSpec, RunOrchestrator
from hermes_agent.repositories import RunRepo


_MAX_LIMIT = 500


def _run_projection(run) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "session_id": run.session_id,
        "status": run.status,
        "started_at": run.started_at,
        "updated_at": run.updated_at,
        "completed_at": run.completed_at,
        "turn_id": run.turn_id,
        "runtime_scope_key": run.runtime_scope_key,
        "runtime_session_id": run.runtime_session_id,
        "last_seq": run.last_seq,
        "terminal_seq": run.terminal_seq,
        "terminal_degraded": run.terminal_degraded,
        "terminal_cause": run.terminal_cause,
    }


def make_method_run_get(repo: RunRepo):
    @requires_permission("run.read", read_only=True)
    def method_run_get(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        run_id = str(params.get("run_id") or "").strip()
        if not run_id:
            raise MethodError(ErrorCode.INVALID_PARAMS, "run_id is required")
        run = repo.get_run(run_id)
        if run is None:
            raise MethodError(
                ErrorCode.RUN_NOT_FOUND, f"run {run_id!r} not found"
            )
        return _run_projection(run)

    return method_run_get


def make_method_run_list(conn_provider):
    """List runs under a session. Reads ``runs`` directly via the caller's
    connection (RunRepo does not yet expose a list API — bypassing at the
    method boundary keeps the Repo surface minimal).
    """

    @requires_permission("run.read", read_only=True)
    def method_run_list(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            raise MethodError(ErrorCode.INVALID_PARAMS, "session_id is required")
        status_filter = params.get("status")
        limit = _int_or_default(params.get("limit"), 100)
        limit = max(1, min(int(limit), _MAX_LIMIT))
        conn = conn_provider(session_id)
        clauses = ["session_id = ?"]
        query_args: list[Any] = [session_id]
        if status_filter:
            if isinstance(status_filter, list):
                placeholders = ",".join("?" for _ in status_filter)
                clauses.append(f"status IN ({placeholders})")
                query_args.extend(str(s) for s in status_filter)
            else:
                clauses.append("status = ?")
                query_args.append(str(status_filter))
        rows = conn.execute(
            f"""
            SELECT run_id, session_id, status, started_at, updated_at,
                   completed_at, turn_id, runtime_scope_key, runtime_session_id,
                   last_seq, terminal_seq, terminal_degraded, terminal_cause
              FROM runs
             WHERE {" AND ".join(clauses)}
             ORDER BY started_at DESC
             LIMIT ?
            """,
            (*query_args, limit),
        ).fetchall()

        return {
            "session_id": session_id,
            "runs": [
                {
                    "run_id": str(row["run_id"]),
                    "session_id": str(row["session_id"]),
                    "status": str(row["status"]),
                    "started_at": float(row["started_at"] or 0),
                    "updated_at": float(row["updated_at"] or 0),
                    "completed_at": (
                        float(row["completed_at"])
                        if row["completed_at"] is not None
                        else None
                    ),
                    "turn_id": str(row["turn_id"] or ""),
                    "runtime_scope_key": str(row["runtime_scope_key"] or ""),
                    "last_seq": int(row["last_seq"] or 0),
                    "terminal_seq": int(row["terminal_seq"] or 0),
                    "terminal_degraded": bool(int(row["terminal_degraded"] or 0)),
                    "terminal_cause": str(row["terminal_cause"] or ""),
                }
                for row in rows
            ],
        }

    return method_run_list


def make_method_run_list_events(repo: RunRepo):
    """Build a ``run.list_events`` handler bound to a specific RunRepo.

    spec §6.4 — ``_internal.*`` events stay filtered by default. Callers who
    truly need them pass ``include_internal=True`` (backend-side debugging
    only; the frontend cursor path never sees them).
    """

    @requires_permission("run.events.read", read_only=True)
    def method_run_list_events(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            raise MethodError(ErrorCode.INVALID_PARAMS, "session_id is required")
        after_seq = _int_or_zero(params.get("after_seq"))
        before_seq = _int_or_zero(params.get("before_seq"))
        limit = _int_or_default(params.get("limit"), 200)
        limit = max(1, min(limit, _MAX_LIMIT))
        types_raw = params.get("types")
        types: set[str] | None = None
        if isinstance(types_raw, list):
            types = {str(t) for t in types_raw if str(t or "").strip()}
        include_internal = bool(params.get("include_internal", False))

        events = repo.list_events(
            session_id,
            after_seq=after_seq,
            before_seq=before_seq,
            types=types,
            include_internal=include_internal,
            limit=limit,
        )
        return {
            "session_id": session_id,
            "events": [
                {
                    "seq": e.seq,
                    "event_type": e.event_type,
                    "run_id": e.run_id,
                    "turn_id": e.turn_id,
                    "timestamp": e.timestamp,
                    "payload": e.payload,
                }
                for e in events
            ],
        }

    return method_run_list_events


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def make_method_run_launch(orch: RunOrchestrator, conn_provider):
    """Build ``run.launch`` — spawn a run via ``RunOrchestrator.launch``.

    ``conn_provider`` returns a ``sqlite3.Connection`` bound to the caller's
    per-session scope. The gateway layer typically resolves this from the
    connection pool inside the sharded RPC lock (spec §8.2).
    """

    @requires_permission("run.write")
    def method_run_launch(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        run_id = str(params.get("run_id") or "").strip()
        session_id = str(params.get("session_id") or "").strip()
        worker_id = str(params.get("worker_id") or "").strip()
        if not run_id or not session_id or not worker_id:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "run_id, session_id, worker_id all required",
            )
        turn_id = str(params.get("turn_id") or "").strip()
        runtime_scope_key = str(params.get("runtime_scope_key") or "").strip()
        conn = conn_provider(session_id)
        result = orch.launch(
            conn,
            RunLaunchSpec(
                run_id=run_id,
                session_id=session_id,
                worker_id=worker_id,
                turn_id=turn_id,
                runtime_scope_key=runtime_scope_key,
            ),
        )
        return {
            "run_id": result.inflight.run_id,
            "session_id": result.inflight.session_id,
            "worker_id": result.inflight.worker_id,
            "start_seq": result.start_seq,
            "allocated_at": result.inflight.allocated_at,
        }

    return method_run_launch


def make_method_run_terminate(orch: RunOrchestrator, conn_provider):
    """Build ``run.terminate`` — atomic terminal transition via spec §7.2."""

    @requires_permission("run.write")
    def method_run_terminate(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        run_id = str(params.get("run_id") or "").strip()
        session_id = str(params.get("session_id") or "").strip()
        target_status = str(params.get("target_status") or "").strip().lower()
        if not run_id or not session_id or not target_status:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                "run_id, session_id, target_status all required",
            )
        cause_raw = str(params.get("cause") or "worker_emitted").strip()
        try:
            cause = TerminateCause(cause_raw)
        except ValueError:
            raise MethodError(
                ErrorCode.INVALID_PARAMS,
                f"cause {cause_raw!r} is not a spec TerminateCause",
            )
        conn = conn_provider(session_id)
        try:
            result = orch.terminate(
                conn,
                run_id=run_id,
                session_id=session_id,
                target_status=target_status,
                cause=cause,
                turn_id=str(params.get("turn_id") or ""),
                message=str(params.get("message") or ""),
            )
        except ValueError as exc:
            raise MethodError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        return {
            "outcome": result.outcome.value,
            "run_id": result.run_id,
            "session_id": result.session_id,
            "terminal_status": result.terminal_status,
            "terminal_seq": result.terminal_seq,
            "cause": result.cause.value,
            "degraded": result.degraded,
        }

    return method_run_terminate


def register(registry: MethodRegistry, repo: RunRepo, conn_provider=None) -> None:
    """Register read-side ``run.*`` methods.

    ``conn_provider`` (optional) enables ``run.list`` — a SQL scan the Repo
    Protocol does not surface. Without it, only ``run.list_events`` +
    ``run.get`` register.
    """
    registry.register("run.list_events", make_method_run_list_events(repo))
    registry.register("run.get", make_method_run_get(repo))
    if conn_provider is not None:
        registry.register("run.list", make_method_run_list(conn_provider))


def make_method_run_reap_orphans(orch: RunOrchestrator, conn_provider):
    """Build ``run.reap_orphans`` — rebuild WorkerPool from active runs.

    Ops path: after a gateway restart, the in-memory pool is empty. Calling
    this method for each known session recovers the inflight state from
    ``runs`` rows that are still active (spec §8.1 crash recovery).
    """

    @requires_permission("run.write")
    def method_run_reap_orphans(
        params: dict[str, Any], ctx: DispatchContext
    ) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "").strip()
        if not session_id:
            raise MethodError(
                ErrorCode.INVALID_PARAMS, "session_id is required"
            )
        conn = conn_provider(session_id)
        recovered = orch.reap_orphans(conn)
        return {
            "session_id": session_id,
            "recovered_run_ids": list(recovered),
            "pool_size": orch.pool.size(),
        }

    return method_run_reap_orphans


def register_lifecycle(
    registry: MethodRegistry,
    orch: RunOrchestrator,
    conn_provider,
) -> None:
    """Register ``run.launch`` + ``run.terminate`` + ``run.reap_orphans``
    (spec §7.2, §8.1).
    """
    registry.register("run.launch", make_method_run_launch(orch, conn_provider))
    registry.register("run.terminate", make_method_run_terminate(orch, conn_provider))
    registry.register(
        "run.reap_orphans", make_method_run_reap_orphans(orch, conn_provider)
    )
